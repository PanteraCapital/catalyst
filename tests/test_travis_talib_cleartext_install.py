# Security regression tests for the Travis before_install TA-Lib install.
#
# They execute the before_install commands from .travis.yml. A local server
# stands in for a network attacker who can replace a cleartext HTTP response.
# The tests fail while those commands unpack and build that substituted
# archive. A fix has to download over HTTPS and reject substituted bytes
# before tar, configure, make, or sudo make install.
#
# Run:
#   python3 -m unittest tests.test_travis_talib_cleartext_install -v

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TRAVIS_FILE = REPO_ROOT / ".travis.yml"
SENTINEL = "TRAVIS_SECURE_SENTINEL_VALUE_528a956b"
SENTINEL_NAME = "TRAVIS_SECURE_SENTINEL"

# Commands that belong to the TA-Lib fetch/build, including a checksum step
# placed next to them. The Miniconda install later in before_install is not
# part of this path.
_INTERESTING = re.compile(
    r"ta-lib|talib|sha256sum|sha384sum|sha512sum|\bshasum\b|"
    r"openssl\s+dgst|hashlib|checksum",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s'\"<>]+")
_CHECKSUM_TOOL = re.compile(
    r"sha256sum|sha384sum|sha512sum|\bshasum\b|openssl\s+dgst",
    re.IGNORECASE,
)
_CHECKSUM_FILE = re.compile(
    r"(?:sha256sum|sha384sum|sha512sum|shasum)\s+.*?-c\s+([^\s;|&]+)",
    re.IGNORECASE,
)

# Travis's configure stage installs this wrapper before before_install.
# travis-build lib/travis/build/appliances/redefine_curl.rb calls
# `command curl --retry 2 -sS` with the original arguments, including the URL.
# travis_cmd (lib/travis/build/bash/travis_cmd.bash) evals each before_install
# command in the job shell and travis_assert stops the job on a non-zero status.
_RUNNER = r"""
function curl() {
  command curl --retry 2 -sS "$@"
}
travis_cmd() {
  eval "$1"
  local result=$?
  if [[ "$result" -ne 0 ]]; then
    exit "$result"
  fi
}
for travis_command in "$@"; do
  travis_cmd "$travis_command"
done
"""


def talib_commands(before_install):
    commands = []
    started = False
    for command in before_install:
        if not isinstance(command, str):
            continue
        if _INTERESTING.search(command):
            started = True
            commands.append(command)
        elif started:
            break
    return commands


def load_talib_commands():
    with TRAVIS_FILE.open() as handle:
        config = yaml.safe_load(handle)
    before_install = config.get("before_install") or []
    return talib_commands(before_install)


def _digest(algorithm, data):
    if algorithm == "sha512":
        return hashlib.sha512(data).hexdigest(), 128
    if algorithm == "sha384":
        return hashlib.sha384(data).hexdigest(), 96
    return hashlib.sha256(data).hexdigest(), 64


def _algorithm_of(command):
    lowered = command.lower()
    if "sha512" in lowered:
        return "sha512"
    if "sha384" in lowered:
        return "sha384"
    return "sha256"


def rewrite_archive_urls(commands, origin):
    """Point TA-Lib archive URLs at origin without changing other commands."""

    def replace(match):
        url = match.group(0)
        if re.search(r"ta-lib.*\.(?:tar\.gz|tgz|tar\.bz2|zip)(?:\?|$)", url, re.I):
            return origin + "/substituted-ta-lib.tar.gz"
        return url

    return [_URL.sub(replace, command) for command in commands]


def rewrite_checksum_literals(commands, benign_bytes):
    """Make an existing pinned digest match benign_bytes.

    Used only so a later fetch can be distinguished from bytes that already
    passed the script's own checksum command. Digests are rewritten only in
    commands that already invoke a checksum tool.
    """
    rewritten = []
    for command in commands:
        if _CHECKSUM_TOOL.search(command):
            digest, width = _digest(_algorithm_of(command), benign_bytes)
            command = re.sub(
                rf"\b[0-9a-fA-F]{{{width}}}\b",
                digest,
                command,
            )
        rewritten.append(command)
    return rewritten


def materialize_checksum_files(commands, workdir, benign_bytes):
    for command in commands:
        if not _CHECKSUM_TOOL.search(command):
            continue
        digest, width = _digest(_algorithm_of(command), benign_bytes)
        for match in _CHECKSUM_FILE.finditer(command):
            reference = match.group(1).strip("'\"")
            if reference == "-":
                continue
            source = Path(reference)
            if not source.is_absolute():
                candidate = REPO_ROOT / reference
                destination = workdir / reference
            else:
                candidate = source
                destination = workdir / source.name
            if not candidate.is_file():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            text = candidate.read_text()
            destination.write_text(
                re.sub(rf"\b[0-9a-fA-F]{{{width}}}\b", digest, text)
            )


def _shell_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_archive(destination, marker, payload, kind):
    """Create a ta-lib-shaped source tree whose build records what ran.

    `kind` is EVIL or BENIGN. EVIL reads the job sentinel (directly, and from
    the sudo process's environment via /proc, because sudo env_reset hides it
    from the child) and records the install uid.
    """
    root = destination.parent / (destination.name + "-src")
    project = root / "ta-lib"
    if root.exists():
        shutil.rmtree(root)
    project.mkdir(parents=True)
    (project / f"{kind}_EXTRACTED").write_text(kind + "\n")
    if kind == "EVIL":
        configure = f"""#!/bin/sh
printf 'CONFIGURE_UID=%s\\n' "$(id -u)" >> {_shell_quote(str(marker))}
printf 'CONFIGURE_SENTINEL=%s\\n' "${SENTINEL_NAME}" >> {_shell_quote(str(marker))}
printf 'PAYLOAD={payload}\\n' >> {_shell_quote(str(marker))}
exit 0
"""
        makefile = f"""MARKER := {marker}
all:
\tprintf 'MAKE_UID=%s\\n' "$$(id -u)" >> $(MARKER)
\tprintf 'MAKE_SENTINEL=%s\\n' "$${SENTINEL_NAME}" >> $(MARKER)
\tprintf 'PAYLOAD={payload}\\n' >> $(MARKER)
install:
\tprintf 'INSTALL_UID=%s\\n' "$$(id -u)" >> $(MARKER)
\tpid=$$$$; \\
\twhile [ "$$pid" -gt 1 ]; do \\
\t  if tr '\\0' '\\n' < /proc/$$pid/environ 2>/dev/null | grep -q '^{SENTINEL_NAME}='; then \\
\t    tr '\\0' '\\n' < /proc/$$pid/environ | grep '^{SENTINEL_NAME}=' >> $(MARKER); \\
\t    break; \\
\t  fi; \\
\t  pid=$$(awk '/^PPid:/ {{print $$2}}' /proc/$$pid/status); \\
\tdone
\tprintf 'PAYLOAD={payload}\\n' >> $(MARKER)
"""
    else:
        configure = f"""#!/bin/sh
printf 'PAYLOAD={payload}\\n' >> {_shell_quote(str(marker))}
exit 0
"""
        makefile = f"""MARKER := {marker}
all:
\tprintf 'PAYLOAD={payload}\\n' >> $(MARKER)
install:
\tprintf 'PAYLOAD={payload}\\n' >> $(MARKER)
"""
    script_path = project / "configure"
    script_path.write_text(configure)
    script_path.chmod(0o755)
    (project / "Makefile").write_text(makefile)
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(project, arcname="ta-lib")
    shutil.rmtree(root)
    return destination.read_bytes()


class _ArchiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        body = self.server.next_body()
        self.server.requests.append(("GET", self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.server.requests.append(("CONNECT", self.path))
        self.send_error(403, "tls tunnel refused")

    def log_message(self, fmt, *args):
        return


class ArchiveServer:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []
        self._index = 0
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        self._httpd.next_body = self._next_body
        self._httpd.requests = self.requests
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _next_body(self):
        with self._lock:
            index = min(self._index, len(self.bodies) - 1)
            body = self.bodies[index]
            self._index += 1
            return body

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


class RunResult:
    def __init__(self, returncode, marker, workdir, requests, stdout, stderr, commands):
        self.returncode = returncode
        self.marker = marker
        self.workdir = workdir
        self.requests = requests
        self.stdout = stdout
        self.stderr = stderr
        self.commands = commands

    def extracted(self, kind):
        return any(path.name == f"{kind}_EXTRACTED" for path in self.workdir.rglob("*"))

    def report(self):
        return (
            f"returncode={self.returncode}\n"
            f"commands={self.commands!r}\n"
            f"requests={self.requests!r}\n"
            f"marker:\n{self.marker}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}\n"
        )


def run_commands(commands, specs, mode):
    """Run before_install commands against attacker-controlled archive bytes.

    `specs` is a list of (kind, payload) pairs. kind is EVIL or BENIGN.
    Each pair becomes one HTTP response, in order.

    mode "cleartext" leaves URLs unchanged and publishes the bodies through an
    HTTP proxy, which is the path curl takes for http:// URLs.
    mode "forced" rewrites TA-Lib archive URLs to the local server so the
    integrity check is exercised even if the configured scheme is https.
    mode "refetch" is "forced" plus a checksum literal rewritten to the first
    body, and later responses are the following bodies.
    """
    workdir = Path(tempfile.mkdtemp(prefix="talib-install-"))
    marker_path = workdir / "attacker-marker.txt"
    marker_path.write_text("")
    marker_path.chmod(0o666)
    bodies = [
        build_archive(workdir / f"{payload}.tar.gz", marker_path, payload, kind)
        for kind, payload in specs
    ]
    server = ArchiveServer(bodies)
    try:
        executed = list(commands)
        env = os.environ.copy()
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy", "no_proxy", "NO_PROXY"):
            env.pop(key, None)
        env[SENTINEL_NAME] = SENTINEL
        if mode == "cleartext":
            proxy = server.origin
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
        else:
            if mode == "refetch":
                executed = rewrite_checksum_literals(executed, bodies[0])
                materialize_checksum_files(executed, workdir, bodies[0])
            executed = rewrite_archive_urls(executed, server.origin)
            # Keep a failed HTTPS fallback from leaving the sandbox.
            env["https_proxy"] = server.origin
            env["HTTPS_PROXY"] = server.origin
        proc = subprocess.run(
            ["bash", "-c", _RUNNER, "bash", *executed],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        marker = marker_path.read_text() if marker_path.exists() else ""
        return RunResult(
            proc.returncode,
            marker,
            workdir,
            list(server.requests),
            proc.stdout,
            proc.stderr,
            executed,
        )
    finally:
        server.close()


def _problems(result):
    problems = []
    if result.extracted("EVIL"):
        problems.append("substituted archive was extracted")
    if "PAYLOAD=EVIL" in result.marker:
        problems.append("substituted configure/Makefile ran")
    if SENTINEL in result.marker:
        problems.append("job environment value was readable by the substituted build")
    if "INSTALL_UID=0" in result.marker:
        problems.append("sudo make install ran the substituted Makefile as root")
    return problems


class TravisTalibCleartextInstallTest(unittest.TestCase):
    def setUp(self):
        self.commands = load_talib_commands()
        self._trees = []

    def tearDown(self):
        for path in self._trees:
            subprocess.run(
                ["sudo", "-n", "rm", "-rf", "--", str(path)],
                check=False,
                capture_output=True,
            )

    def _run(self, specs, mode):
        result = run_commands(self.commands, specs, mode)
        self._trees.append(result.workdir)
        return result

    def test_unmodified_before_install_rejects_cleartext_substitution(self):
        result = self._run([("EVIL", "EVIL")], mode="cleartext")
        self.assertEqual(_problems(result), [], result.report())

    def test_attacker_bytes_are_not_unpacked_or_installed(self):
        # Two different substituted archives. A checksum of the first response
        # must reject it before extract. A second fetch must not be installed
        # either: both bodies are attacker-controlled and fail the assertions.
        result = self._run([("EVIL", "EVIL"), ("EVIL", "EVIL2")], mode="forced")
        self.assertEqual(_problems(result), [], result.report())

    def test_checksum_does_not_use_a_later_download(self):
        self.assertTrue(
            any(_CHECKSUM_TOOL.search(command) for command in self.commands),
            "before_install never checksums the TA-Lib archive before building it: "
            + repr(self.commands),
        )
        # First response is the archive the checksum is pointed at. Every later
        # response is a different archive. Unpacking or building the later
        # bytes means the check and the build did not use the same download.
        result = self._run([("BENIGN", "BENIGN"), ("EVIL", "EVIL")], mode="refetch")
        self.assertEqual(_problems(result), [], result.report())

    def test_talib_archive_urls_use_https(self):
        urls = []
        for command in self.commands:
            urls.extend(
                url
                for url in _URL.findall(command)
                if re.search(r"ta-lib", url, re.IGNORECASE)
            )
        self.assertTrue(urls, "no TA-Lib archive URL in before_install")
        for url in urls:
            self.assertTrue(
                url.startswith("https://"),
                f"TA-Lib archive URL is not HTTPS: {url}",
            )


if __name__ == "__main__":
    unittest.main()
