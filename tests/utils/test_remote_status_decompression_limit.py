"""Status responses must not be inflated without an output cap.

``decompress_data`` base64-decodes a status field and calls
``zlib.decompress`` with no maximum output length. ``load_response`` sends
both the ``log`` field and the ``data`` field (the performance frame)
through that helper. ``handle_status``, ``handle_response`` (status mode),
and ``get_remote_status`` all reach it.

Contract these tests lock in:

* A decompressed log or performance payload of ``MAX_DECOMPRESSED_BYTES``
  (8 MiB) or less is returned, including under the address-space cap below.
* Anything larger is refused with an exception other than ``MemoryError``.
* The refusal happens before the full output is allocated. A 64 MiB stream
  is inflated inside a process whose address space may grow by only 32 MiB
  after import, which is enough for an 8 MiB streaming cap and not enough
  for the whole bomb.
* The compressed body used for the oversized cases is under 256 KiB, so a
  check on the encoded size alone does not count as a fix.
* Refusing the payload does not create a file in the working directory.

``catalyst.__main__.remote_status`` is not imported here. Loading it pulls
in the full application stack. It calls ``get_remote_status`` and only
writes ``log_output`` / ``data_output`` after that call returns, so the
decompression these tests exercise is the one the command reaches.

The HTTP body is parsed with the same steps ``requests`` 2.18.4
``Response.json`` uses for a UTF-8 JSON document whose encoding is unset
and whose first four bytes contain no NUL: decode as UTF-8, then
``json.loads``. ``requests`` does not transform the ``log`` or ``data``
strings. ``send_digest_request`` is replaced so the suite does not contact
the status host; the object it returns is the parsed body above.
"""
from __future__ import print_function

import base64
import json
import os
import pickle
import resource
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib
from uuid import uuid4

MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024
BOMB_BYTES = 64 * 1024 * 1024
# Measured on this runtime: zlib.decompressobj(..., max_length=8MiB) of a
# 64 MiB stream needs about 20 MiB of address space above the interpreter.
# 32 MiB leaves that room and still faults a full 64 MiB inflate.
HEADROOM_BYTES = 32 * 1024 * 1024
MAX_COMPRESSED_INPUT = 256 * 1024
SMALL_TEXT = "remote status ok\n"
SMALL_FRAME = {"ok": True, "rows": 1}


class StatusHttpResponse(object):
    """Body parser matching requests 2.18.4 for this suite's JSON documents.

    ``Response.json`` (requests/models.py) calls ``guess_json_utf`` when no
    encoding was set and the body is longer than 3 bytes. ``guess_json_utf``
    (requests/utils.py) returns ``utf-8`` when the first four bytes contain
    no NUL. ``json.loads`` then receives that decoded text. The status
    documents built here are ASCII JSON, so the field strings are unchanged.
    """

    def __init__(self, body):
        self.status_code = 200
        self.encoding = None
        self.headers = {}
        self._content = body

    @property
    def content(self):
        return self._content

    def json(self):
        sample = self.content[:4]
        if sample.count(b"\x00") != 0:
            raise AssertionError("status fixture must be utf-8 json")
        if len(self.content) <= 3:
            raise AssertionError(
                "status fixture must take the utf-8 json path"
            )
        return json.loads(self.content.decode("utf-8"))


def _compress_stream(chunks):
    compressor = zlib.compressobj(9)
    parts = []
    for chunk in chunks:
        parts.append(compressor.compress(chunk))
    parts.append(compressor.flush())
    return b"".join(parts)


def _repeated_chunks(size, chunk_size=256 * 1024):
    chunk = b"A" * chunk_size
    left = size
    while left:
        take = min(left, chunk_size)
        yield chunk[:take]
        left -= take


def _b64(raw_compressed):
    encoded = base64.b64encode(raw_compressed).decode("ascii")
    if len(encoded) >= MAX_COMPRESSED_INPUT:
        raise AssertionError(
            "fixture compressed to %s bytes; a size check on the encoded "
            "body could hide an unbounded inflate" % len(encoded)
        )
    return encoded


def encoded_text(size):
    return _b64(_compress_stream(_repeated_chunks(size)))


def encoded_pickle_bytes(size):
    """Protocol-3 pickle of ``size`` 0x41 bytes, compressed without holding it.

    Layout matches ``pickle.dumps(payload, protocol=3)`` for a ``bytes``
    object that fits in a 4-byte length: PROTO, BINBYTES, payload, STOP.
    """
    header = b"\x80\x03" + b"B" + struct.pack("<I", size)

    def chunks():
        yield header
        for piece in _repeated_chunks(size):
            yield piece
        yield b"."

    return _b64(_compress_stream(chunks()))


def encoded_small_text():
    return _b64(zlib.compress(SMALL_TEXT.encode("utf-8")))


def encoded_small_frame():
    return _b64(zlib.compress(pickle.dumps(SMALL_FRAME, protocol=2)))


def parsed_status(log_field, data_field):
    body = json.dumps(
        {"status": "running", "log": log_field, "data": data_field}
    ).encode("utf-8")
    return StatusHttpResponse(body).json()


def _vm_size_bytes():
    with open("/proc/self/status", "r") as status:
        for line in status:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("VmSize missing from /proc/self/status")


def _cap_address_space():
    limit = _vm_size_bytes() + HEADROOM_BYTES
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _snapshot_cwd():
    return set(os.listdir(os.getcwd()))


def _refuse_if_files_appeared(before):
    created = set(os.listdir(os.getcwd())) - before
    if created:
        sys.stdout.write("SIDE_EFFECT %s\n" % sorted(created))
        return True
    return False


def _load_remote_modules():
    """Execute the real remote modules.

    A full ``import catalyst`` also imports the trading stack. When that
    stack is not installed, load the two remote modules from their source
    paths with stubs for the imports they do not use on this path.
    """
    import importlib.util

    if (
        importlib.util.find_spec("pandas") is not None
        and importlib.util.find_spec("logbook") is not None
        and importlib.util.find_spec("requests") is not None
    ):
        from catalyst.utils import remote as remote_mod
        from catalyst.utils import remote_utils as remote_utils_mod
        return remote_utils_mod, remote_mod

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    )

    def package(name, path):
        module = types.ModuleType(name)
        module.__path__ = [path]
        module.__package__ = name
        module.__file__ = os.path.join(path, "__init__.py")
        sys.modules[name] = module
        return module

    package("catalyst", os.path.join(repo_root, "catalyst"))
    package("catalyst.utils", os.path.join(repo_root, "catalyst", "utils"))
    package(
        "catalyst.exchange",
        os.path.join(repo_root, "catalyst", "exchange"),
    )
    package(
        "catalyst.exchange.utils",
        os.path.join(repo_root, "catalyst", "exchange", "utils"),
    )

    pandas = types.ModuleType("pandas")

    class Timestamp(object):
        pass

    pandas.Timestamp = Timestamp
    sys.modules["pandas"] = pandas

    logbook = types.ModuleType("logbook")

    class Logger(object):
        def __init__(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    logbook.Logger = Logger
    sys.modules["logbook"] = logbook

    requests = types.ModuleType("requests")
    requests.Session = object
    requests_utils = types.ModuleType("requests.utils")
    requests_utils.parse_dict_header = lambda value: {}
    requests.utils = requests_utils
    sys.modules["requests"] = requests
    sys.modules["requests.utils"] = requests_utils

    exchange_utils = types.ModuleType("catalyst.exchange.utils.exchange_utils")
    exchange_utils.get_remote_auth = lambda *args, **kwargs: {
        "key": "k",
        "secret": "s",
    }
    exchange_utils.get_remote_folder = lambda *args, **kwargs: os.getcwd()
    sys.modules["catalyst.exchange.utils.exchange_utils"] = exchange_utils

    exchange_errors = types.ModuleType("catalyst.exchange.exchange_errors")

    class RemoteAuthEmpty(Exception):
        def __init__(self, **kwargs):
            super(RemoteAuthEmpty, self).__init__(kwargs)

    exchange_errors.RemoteAuthEmpty = RemoteAuthEmpty
    sys.modules["catalyst.exchange.exchange_errors"] = exchange_errors

    spec = importlib.util.spec_from_file_location(
        "catalyst.utils.remote_utils",
        os.path.join(repo_root, "catalyst", "utils", "remote_utils.py"),
    )
    remote_utils_mod = importlib.util.module_from_spec(spec)
    sys.modules["catalyst.utils.remote_utils"] = remote_utils_mod
    spec.loader.exec_module(remote_utils_mod)

    spec = importlib.util.spec_from_file_location(
        "catalyst.utils.remote",
        os.path.join(repo_root, "catalyst", "utils", "remote.py"),
    )
    remote_mod = importlib.util.module_from_spec(spec)
    sys.modules["catalyst.utils.remote"] = remote_mod
    spec.loader.exec_module(remote_mod)
    return remote_utils_mod, remote_mod


def _materialized_size(value):
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, tuple):
        return sum(_materialized_size(item) for item in value)
    return 0


def _call_bounded(fn):
    before = _snapshot_cwd()
    try:
        value = fn()
    except MemoryError:
        sys.stdout.write("MEMORY\n")
        return 2
    except Exception as exc:
        if _refuse_if_files_appeared(before):
            return 6
        sys.stdout.write(
            "REJECTED %s.%s\n" % (type(exc).__module__, type(exc).__name__)
        )
        return 0
    if _refuse_if_files_appeared(before):
        return 6
    sys.stdout.write("EXPANDED %s\n" % _materialized_size(value))
    return 3


def _call_expected(fn, check):
    before = _snapshot_cwd()
    try:
        _cap_address_space()
        value = fn()
    except MemoryError:
        sys.stdout.write("MEMORY\n")
        return 2
    except Exception as exc:
        sys.stdout.write(
            "ERROR %s.%s %s\n"
            % (type(exc).__module__, type(exc).__name__, exc)
        )
        return 4
    if _refuse_if_files_appeared(before):
        return 6
    if not check(value):
        sys.stdout.write("BAD\n")
        return 5
    sys.stdout.write("OK\n")
    return 0


def _log_payload(size):
    return parsed_status(encoded_text(size), None)


def _data_payload(size):
    return parsed_status(encoded_small_text(), encoded_pickle_bytes(size))


def _invoke(action, payload, remote_utils_mod, remote_mod):
    if action == "decompress":
        return remote_utils_mod.decompress_data(payload["log"])
    if action == "log":
        _data, log_file = remote_utils_mod.load_response(payload)
        return log_file
    if action == "data":
        data, _log_file = remote_utils_mod.load_response(payload)
        return data
    if action == "handle-status-log":
        return remote_utils_mod.handle_status(payload)
    if action == "handle-status-data":
        return remote_utils_mod.handle_status(payload)
    if action == "handle-response-log":
        return remote_mod.handle_response(
            _response_from_payload(payload), remote_mod.STATUS
        )
    if action == "handle-response-data":
        return remote_mod.handle_response(
            _response_from_payload(payload), remote_mod.STATUS
        )
    if action in ("get-remote-status-log", "get-remote-status-data"):
        body = json.dumps(payload).encode("utf-8")
        response = StatusHttpResponse(body)

        def send_digest_request(json_file, path, method):
            if path != remote_mod.STATUS_PATH or method != remote_mod.GET:
                raise AssertionError("status request was not a status GET")
            if "algo_id" not in json_file:
                raise AssertionError("status request omitted algo_id")
            return response

        remote_mod.send_digest_request = send_digest_request
        return remote_mod.get_remote_status(uuid4().hex)
    raise AssertionError("unknown action %s" % action)


def _response_from_payload(payload):
    return StatusHttpResponse(json.dumps(payload).encode("utf-8"))


def _check_accept(action, value):
    if action == "decompress":
        return value == SMALL_TEXT.encode("utf-8")
    if action == "log":
        return value == SMALL_TEXT
    if action == "data":
        return value == SMALL_FRAME
    if action in (
        "handle-status-log",
        "handle-response-log",
        "get-remote-status-log",
    ):
        status, perf, log_file = value
        return status == "running" and perf is None and log_file == SMALL_TEXT
    if action in (
        "handle-status-data",
        "handle-response-data",
        "get-remote-status-data",
    ):
        status, perf, log_file = value
        return (
            status == "running"
            and perf == SMALL_FRAME
            and log_file == SMALL_TEXT
        )
    return False


def worker(action):
    # Prove the hand-built performance pickle is what pickle.loads reads,
    # before any product code sees a hostile payload.
    sample = zlib.decompress(base64.b64decode(encoded_pickle_bytes(10)))
    if pickle.loads(sample) != b"A" * 10:
        sys.stdout.write("FIXTURE\n")
        return 7

    remote_utils_mod, remote_mod = _load_remote_modules()
    rejecting = action.startswith("reject-")
    kind = action[len("reject-"):] if rejecting else action[len("accept-"):]

    def invoke(payload):
        return _invoke(kind, payload, remote_utils_mod, remote_mod)

    if not rejecting:
        if kind == "decompress":
            payload = {"log": encoded_small_text(), "data": None}
        elif kind in (
            "log",
            "handle-status-log",
            "handle-response-log",
            "get-remote-status-log",
        ):
            payload = parsed_status(encoded_small_text(), None)
        else:
            payload = parsed_status(
                encoded_small_text(), encoded_small_frame()
            )
        sys.stdout.write("COMPRESSED %s\n" % len(payload["log"]))
        return _call_expected(
            lambda: invoke(payload),
            lambda value: _check_accept(kind, value),
        )

    over = _over_payload(kind)
    sys.stdout.write("COMPRESSED %s\n" % _encoded_len(kind, over))
    over_outcome = _call_bounded(lambda: invoke(over))

    # Build the bomb before lowering the address-space cap. The cap is what
    # turns an unbounded inflate into MemoryError instead of a multi-megabyte
    # string the checker would then have to hold.
    bomb = _bomb_payload(kind)
    _cap_address_space()
    sys.stdout.write("COMPRESSED %s\n" % _encoded_len(kind, bomb))
    bomb_outcome = _call_bounded(lambda: invoke(bomb))
    if over_outcome != 0:
        return over_outcome
    return bomb_outcome


def _over_payload(kind):
    size = MAX_DECOMPRESSED_BYTES + 1
    if kind in ("data", "handle-status-data", "handle-response-data",
                "get-remote-status-data"):
        return _data_payload(size)
    if kind == "decompress":
        return {"log": encoded_text(size), "data": None}
    return _log_payload(size)


def _bomb_payload(kind):
    if kind in ("data", "handle-status-data", "handle-response-data",
                "get-remote-status-data"):
        return _data_payload(BOMB_BYTES)
    if kind == "decompress":
        return {"log": encoded_text(BOMB_BYTES), "data": None}
    return _log_payload(BOMB_BYTES)


def _encoded_len(kind, payload):
    if kind in ("data", "handle-status-data", "handle-response-data",
                "get-remote-status-data"):
        return len(payload["data"])
    return len(payload["log"])


@unittest.skipUnless(
    sys.version_info >= (3, 6),
    "status decompression worker requires Python 3.6 or newer",
)
class RemoteStatusDecompressionLimitTests(unittest.TestCase):
    def _run(self, action):
        tmp = tempfile.mkdtemp(prefix="status-decomp-")
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", action],
            cwd=tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        stdout, stderr = proc.communicate(timeout=120)
        proc.stdout = stdout
        proc.stderr = stderr
        leftover = sorted(os.listdir(tmp))
        if leftover:
            raise AssertionError("worker left files behind: %s" % leftover)
        os.rmdir(tmp)
        return proc

    def _assert_accepted(self, action):
        proc = self._run(action)
        self.assertEqual(
            proc.returncode,
            0,
            "stdout=%r stderr=%r" % (proc.stdout, proc.stderr),
        )
        self.assertIn("OK", proc.stdout.splitlines())
        self.assertNotIn("SIDE_EFFECT", proc.stdout)

    def _assert_rejected(self, action):
        proc = self._run(action)
        message = "stdout=%r stderr=%r code=%s" % (
            proc.stdout,
            proc.stderr,
            proc.returncode,
        )
        self.assertEqual(proc.returncode, 0, message)
        lines = proc.stdout.splitlines()
        rejected = sum(
            1 for line in lines if line.startswith("REJECTED")
        )
        self.assertGreaterEqual(rejected, 2, message)
        self.assertFalse(
            any(line.startswith("MEMORY") for line in lines), message
        )
        self.assertFalse(
            any(line.startswith("EXPANDED") for line in lines), message
        )
        self.assertFalse(
            any(line.startswith("SIDE_EFFECT") for line in lines), message
        )
        compressed = [
            int(line.split()[1])
            for line in lines if line.startswith("COMPRESSED ")
        ]
        self.assertGreaterEqual(len(compressed), 2, message)
        for size in compressed:
            self.assertLess(size, MAX_COMPRESSED_INPUT, message)

    def test_small_log_round_trip_decompress_data(self):
        self._assert_accepted("accept-decompress")

    def test_small_log_round_trip_load_response(self):
        self._assert_accepted("accept-log")

    def test_small_performance_round_trip_load_response(self):
        self._assert_accepted("accept-data")

    def test_small_log_round_trip_handle_status(self):
        self._assert_accepted("accept-handle-status-log")

    def test_small_performance_round_trip_handle_status(self):
        self._assert_accepted("accept-handle-status-data")

    def test_small_log_round_trip_handle_response(self):
        self._assert_accepted("accept-handle-response-log")

    def test_small_performance_round_trip_handle_response(self):
        self._assert_accepted("accept-handle-response-data")

    def test_small_log_round_trip_get_remote_status(self):
        self._assert_accepted("accept-get-remote-status-log")

    def test_small_performance_round_trip_get_remote_status(self):
        self._assert_accepted("accept-get-remote-status-data")

    def test_decompress_data_rejects_output_past_cap(self):
        self._assert_rejected("reject-decompress")

    def test_load_response_log_rejects_output_past_cap(self):
        self._assert_rejected("reject-log")

    def test_load_response_performance_rejects_output_past_cap(self):
        self._assert_rejected("reject-data")

    def test_handle_status_log_rejects_output_past_cap(self):
        self._assert_rejected("reject-handle-status-log")

    def test_handle_status_performance_rejects_output_past_cap(self):
        self._assert_rejected("reject-handle-status-data")

    def test_handle_response_log_rejects_output_past_cap(self):
        self._assert_rejected("reject-handle-response-log")

    def test_handle_response_performance_rejects_output_past_cap(self):
        self._assert_rejected("reject-handle-response-data")

    def test_get_remote_status_log_rejects_output_past_cap(self):
        self._assert_rejected("reject-get-remote-status-log")

    def test_get_remote_status_performance_rejects_output_past_cap(self):
        self._assert_rejected("reject-get-remote-status-data")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        sys.exit(worker(sys.argv[2]))
    unittest.main()
