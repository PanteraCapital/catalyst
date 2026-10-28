# These tests describe the secure behavior of the official Catalyst image.
# They fail while the image still boots Jupyter as root on every interface
# with the documented password. They are expected to pass once that is fixed.
#
# The notebook package under test is notebook 5.6.0 (tornado 5.1, traitlets
# 4.3.2), which is what ``pip install jupyter`` resolved to on the commit
# date: jupyter 1.0.0 depends on an unpinned ``notebook``, and 5.6.0 was the
# newest release on 2018-09-13.

from __future__ import print_function

import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.parse
import warnings
from datetime import timedelta
from http.cookies import SimpleCookie

warnings.filterwarnings("ignore", category=ResourceWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DOCKERFILE = os.path.join(REPO_ROOT, "Dockerfile")
DOCKERFILE_DEV = os.path.join(REPO_ROOT, "Dockerfile-dev")
STARTUP_SCRIPT = os.path.join(REPO_ROOT, "etc", "docker_cmd.sh")
DOCUMENTED_PASSWORD = "jupyter"
EXCHANGE_SENTINEL = "SENTINEL_SECRET_9f3c2a"
NOTEBOOK_PORT = 8888


def _isolate_in_network_namespace():
    """Re-exec inside a user, mount, and network namespace.

    The image entrypoint listens on every interface. The namespace keeps
    that socket off the host network, and the user mapping makes the
    process euid 0 so NotebookApp.allow_root is actually exercised.
    """
    if os.environ.get("CATALYST_TEST_ISOLATED") == "1":
        return
    os.execvp(
        "unshare",
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--mount",
            "env",
            "CATALYST_TEST_ISOLATED=1",
            sys.executable,
            "-m",
            "unittest",
            "tests.security.test_docker_jupyter_exposure",
        ],
    )


_isolate_in_network_namespace()


def _joined_dockerfile(path):
    return open(path).read().replace("\\\n", "")


def dockerfile_env(path):
    """Return ENV assignments from a Dockerfile.

    Quoted values follow the buildkit shell lexer used by Docker 18.06
    (moby/buildkit frontend/dockerfile/shell/lex.go processDoubleQuote):
    characters inside double quotes, including single quotes, are literal.
    """
    env = {}
    for raw in _joined_dockerfile(path).splitlines():
        line = raw.strip()
        if not line.startswith("ENV "):
            continue
        words = _shell_words(line[len("ENV "):])
        if len(words) == 1 and "=" in words[0]:
            key, value = words[0].split("=", 1)
            env[key] = value
            continue
        for word in words:
            if "=" not in word:
                continue
            key, value = word.split("=", 1)
            env[key] = value
    return env


def _shell_words(text):
    words = []
    buf = []
    in_word = False
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is None and ch in ("'", '"'):
            quote = ch
            in_word = True
            i += 1
            continue
        if quote is not None:
            if ch == quote:
                quote = None
                i += 1
                continue
            if quote == '"' and ch == "\\" and i + 1 < len(text) and text[i + 1] in ('"', "$", "\\"):
                buf.append(text[i + 1])
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if ch.isspace():
            if in_word:
                words.append("".join(buf))
                buf = []
                in_word = False
            i += 1
            continue
        buf.append(ch)
        in_word = True
        i += 1
    if quote is not None:
        raise AssertionError("unclosed quote in Dockerfile ENV: %r" % text)
    if in_word:
        words.append("".join(buf))
    return words


def documented_port_bindings(path):
    """Host/container port bindings from documented ``docker run -p`` flags.

    Parsing matches docker/go-connections nat.splitParts and ParsePortSpec
    at the revision Docker 18.06 vendors (7beb39f). An empty host IP is the
    input that libnetwork's bridge driver replaces with 0.0.0.0.
    """
    bindings = []
    for raw in _joined_dockerfile(path).splitlines():
        if "docker run" not in raw or "-p " not in raw:
            continue
        parts = raw.split()
        for index, part in enumerate(parts):
            if part == "-p" and index + 1 < len(parts):
                bindings.append(_split_port_spec(parts[index + 1]))
            elif part.startswith("-p") and part != "-p":
                bindings.append(_split_port_spec(part[2:]))
    return bindings


def _split_port_spec(spec):
    host_ip, host_port, container = _split_port_parts(spec)
    if "/" in container:
        container_port, proto = container.split("/", 1)
    else:
        container_port, proto = container, "tcp"
    return {
        "host_ip": host_ip,
        "host_port": host_port,
        "container_port": container_port,
        "proto": proto,
        "raw": spec,
    }


def _split_port_parts(raw):
    parts = raw.split(":")
    n = len(parts)
    container = parts[-1]
    if n == 1:
        return "", "", container
    if n == 2:
        return "", parts[0], container
    if n == 3:
        return parts[0], parts[1], container
    return ":".join(parts[:-2]), parts[-2], container


def _loopback(ip):
    if ip.startswith("[") and ip.endswith("]"):
        ip = ip[1:-1]
    try:
        return ipaddress_is_loopback(ip)
    except ValueError:
        return False


def ipaddress_is_loopback(ip):
    import ipaddress
    return ipaddress.ip_address(ip).is_loopback


def _bring_up_loopback():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    import fcntl
    import struct
    ifr = struct.pack("16sH14s", b"lo", 0, b"\x00" * 14)
    flags = struct.unpack("16sH14s", fcntl.ioctl(sock, 0x8913, ifr))[1]
    fcntl.ioctl(sock, 0x8914, struct.pack("16sH14s", b"lo", flags | 0x1, b"\x00" * 14))
    sock.close()


def _mount(source, target, fstype, flags):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    result = libc.mount(source, target, fstype, flags, None)
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, "mount %s on %s failed: %s" % (source, target, os.strerror(err)))


def _openssl_conf():
    """Allow the TLS 1.0 method notebook 5.6.0 configures.

    NotebookApp.init_webapp sets ssl_version to ssl.PROTOCOL_TLSv1. OpenSSL 3
    refuses that handshake unless the security level is lowered. The python:3.6
    base image in the Dockerfile does not ship OpenSSL 3.
    """
    path = os.path.join(tempfile.gettempdir(), "catalyst-openssl-tls1.cnf")
    with open(path, "w") as handle:
        handle.write(textwrap.dedent("""\
            openssl_conf = openssl_init
            [openssl_init]
            ssl_conf = ssl_sect
            [ssl_sect]
            system_default = system_default_sect
            [system_default_sect]
            MinProtocol = TLSv1
            CipherString = DEFAULT@SECLEVEL=0
            """))
    return path


def _client_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    return ctx


def _proc_addrs(pid):
    inodes = set()
    fd_dir = "/proc/%s/fd" % pid
    for name in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:["):-1])
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(table).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            if fields[9] not in inodes:
                continue
            found.append(_parse_proc_addr(fields[1]))
    return found


def _parse_proc_addr(hexaddr):
    ip_hex, port_hex = hexaddr.split(":")
    port = int(port_hex, 16)
    raw = bytes.fromhex(ip_hex)
    if len(raw) == 4:
        ip = socket.inet_ntop(socket.AF_INET, raw[::-1])
    else:
        words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
        ip = socket.inet_ntop(socket.AF_INET6, b"".join(words))
    return ip, port


def _server_uid(pid):
    for line in open("/proc/%s/status" % pid):
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise AssertionError("no Uid line for pid %s" % pid)


class _Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header_values(self, name):
        return [value for key, value in self.headers if key.lower() == name.lower()]


class NotebookClient(object):
    def __init__(self, port):
        self.port = port
        self.context = _client_ssl_context()
        self.jar = {}

    def request(self, method, path, body=None, extra_headers=None):
        headers = {}
        if self.jar:
            headers["Cookie"] = "; ".join("%s=%s" % item for item in self.jar.items())
        if extra_headers:
            headers.update(extra_headers)
        conn = http.client.HTTPSConnection(
            "127.0.0.1", self.port, context=self.context, timeout=10)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        result = _Response(resp.status, resp.getheaders(), payload)
        for value in result.header_values("Set-Cookie"):
            cookie = SimpleCookie()
            cookie.load(value)
            for name, morsel in cookie.items():
                self.jar[name] = morsel.value
        conn.close()
        return result

    def login(self, password):
        page = self.request("GET", "/login")
        xsrf = self.jar.get("_xsrf", "")
        body = urllib.parse.urlencode({"password": password, "_xsrf": xsrf}).encode("ascii")
        return self.request(
            "POST",
            "/login",
            body=body,
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        ), page

    def open_terminal_and_read(self, command):
        xsrf = self.jar["_xsrf"]
        body = urllib.parse.urlencode({"_xsrf": xsrf}).encode("ascii")
        created = self.request(
            "POST",
            "/api/terminals",
            body=body,
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Xsrftoken": xsrf,
            },
        )
        if created.status != 200:
            return created.status, created.body.decode("utf-8", "replace")
        name = json.loads(created.body.decode("utf-8"))["name"]
        return 200, self._terminal_output(name, command)

    def _terminal_output(self, name, command):
        from tornado import gen, ioloop
        from tornado.httpclient import HTTPRequest
        from tornado.websocket import websocket_connect

        url = "wss://127.0.0.1:%s/terminals/websocket/%s" % (self.port, name)
        cookie = "; ".join("%s=%s" % item for item in self.jar.items())

        async def run():
            request = HTTPRequest(
                url,
                headers={"Cookie": cookie},
                validate_cert=False,
                request_timeout=10,
                ssl_options=_client_ssl_context(),
            )
            ws = await gen.with_timeout(timedelta(seconds=10), websocket_connect(request))
            await ws.write_message(json.dumps(["stdin", command]))
            chunks = []
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    message = await gen.with_timeout(
                        timedelta(seconds=1), ws.read_message())
                except gen.TimeoutError:
                    continue
                if message is None:
                    break
                chunks.append(message)
                if EXCHANGE_SENTINEL in message:
                    break
            ws.close()
            return "".join(chunks)

        return ioloop.IOLoop.current().run_sync(run)


class ImageState(object):
    server = None
    log_path = None
    root_dir = None
    secret_path = None
    env = None


def _child_pids(pid):
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = open("/proc/%s/stat" % name).read()
        except OSError:
            continue
        # comm may contain spaces and parentheses; ppid is the field after the
        # closing parenthesis of comm.
        after = stat.rsplit(")", 1)[-1].split()
        # Fields after comm are state, ppid.
        if len(after) > 1 and after[1] == str(pid):
            found.append(int(name))
    return found


def _jupyter_pid(root_pid):
    pending = [root_pid]
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            cmd = open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\x00", b" ")
        except OSError:
            cmd = b""
        if b"jupyter-notebook" in cmd or b"notebook.notebookapp" in cmd:
            return pid
        pending.extend(_child_pids(pid))
    return None


def setUpModule():
    warnings.simplefilter("ignore", ResourceWarning)
    warnings.simplefilter("ignore", DeprecationWarning)
    if not os.path.isdir("/projects"):
        subprocess.check_call(["sudo", "mkdir", "-p", "/projects"])
        subprocess.check_call(["sudo", "chmod", "1777", "/projects"])
    _bring_up_loopback()
    state = ImageState()
    state.root_dir = tempfile.mkdtemp(prefix="catalyst-root-")
    state.log_path = os.path.join(tempfile.gettempdir(), "catalyst-jupyter-exposure.log")
    _mount(state.root_dir.encode("utf-8"), b"/root", None, 4096)
    _mount(b"tmpfs", b"/var/tmp", b"tmpfs", 0)
    secret_dir = "/root/.catalyst/data/exchanges/demo"
    os.makedirs(secret_dir)
    state.secret_path = os.path.join(secret_dir, "auth.json")
    with open(state.secret_path, "w") as handle:
        handle.write(json.dumps({
            "name": "demo",
            "key": "SENTINEL_KEY_9f3c2a",
            "secret": EXCHANGE_SENTINEL,
        }))
    os.chmod(state.secret_path, 0o600)

    image_env = dockerfile_env(DOCKERFILE)
    env = {}
    for key, value in os.environ.items():
        if key.startswith("JUPYTER") or key in ("HOME", "IPYTHONDIR"):
            continue
        env[key] = value
    env.update(image_env)
    env["HOME"] = "/root"
    env["USER"] = "root"
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin"
    env["OPENSSL_CONF"] = _openssl_conf()
    state.env = env

    log = open(state.log_path, "w")
    state.server = subprocess.Popen(
        ["bash", STARTUP_SCRIPT],
        cwd=image_env.get("PROJECT_DIR", "/projects"),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    deadline = time.time() + 30
    while time.time() < deadline:
        if state.server.poll() is not None:
            break
        jupyter_pid = _jupyter_pid(state.server.pid)
        if jupyter_pid and _proc_addrs(jupyter_pid):
            break
        time.sleep(0.2)
    ImageState.shared = state


def tearDownModule():
    state = getattr(ImageState, "shared", None)
    if state is None:
        return
    if state.server is not None and state.server.poll() is None:
        try:
            os.killpg(state.server.pid, signal.SIGKILL)
        except OSError:
            pass
        state.server.wait()
    if state.root_dir and os.path.isdir(state.root_dir):
        shutil.rmtree(state.root_dir, ignore_errors=True)


def _log_text():
    state = ImageState.shared
    try:
        return open(state.log_path).read()
    except OSError:
        return ""


class DockerJupyterExposureTests(unittest.TestCase):
    def test_generated_config_rejects_documented_password(self):
        from notebook.auth.security import passwd_check
        from traitlets.config.loader import PyFileConfigLoader

        config_path = ImageState.shared.env["CONFIG_PATH"]
        self.assertTrue(
            os.path.isfile(config_path),
            "startup did not write %s\n%s" % (config_path, _log_text()[-2000:]),
        )
        loader = PyFileConfigLoader(
            os.path.basename(config_path), os.path.dirname(config_path))
        loaded = loader.load_config()
        hashed = loaded.get("NotebookApp", {}).get("password", "")
        self.assertFalse(
            passwd_check(hashed, DOCUMENTED_PASSWORD),
            "first boot installed a Jupyter password that accepts %r: %r"
            % (DOCUMENTED_PASSWORD, hashed),
        )

    def test_documented_password_cannot_read_exchange_credentials(self):
        client = NotebookClient(int(ImageState.shared.env.get("NOTEBOOK_PORT", NOTEBOOK_PORT)))
        try:
            login, _page = client.login(DOCUMENTED_PASSWORD)
        except (socket.error, ssl.SSLError, http.client.HTTPException) as exc:
            login = None
            login_error = exc
        else:
            login_error = None
        accepted = login is not None and login.status in (200, 302) and any(
            name.startswith("username-") for name in client.jar
        )
        transcript = ""
        if accepted:
            try:
                _status, transcript = client.open_terminal_and_read(
                    "cat %s\n" % ImageState.shared.secret_path)
            except Exception as exc:
                transcript = "terminal failed after login: %s" % exc
        self.assertFalse(
            accepted or EXCHANGE_SENTINEL in transcript,
            "password %r authenticated (status %s) and the terminal returned:\n%s\nlog:\n%s"
            % (
                DOCUMENTED_PASSWORD,
                getattr(login, "status", login_error),
                transcript[-1500:],
                _log_text()[-1500:],
            ),
        )

    def test_notebook_does_not_listen_on_all_interfaces_as_root(self):
        state = ImageState.shared
        if state.server.poll() is not None:
            # Refusing to boot as root, or refusing to boot without an
            # operator-supplied secret, is the safe outcome.
            jupyter_pid = None
        else:
            jupyter_pid = _jupyter_pid(state.server.pid)
        if jupyter_pid is None:
            self.assertIsNotNone(
                state.server.poll(),
                "startup is still running but no jupyter process was found\n%s"
                % _log_text()[-1500:],
            )
            return
        uid = _server_uid(jupyter_pid)
        addresses = _proc_addrs(jupyter_pid)
        public = [(ip, port) for ip, port in addresses if not _loopback(ip)]
        argv = open("/proc/%s/cmdline" % jupyter_pid, "rb").read()
        self.assertFalse(
            uid == 0 or public,
            "jupyter pid %s euid %s listening on %s argv %s"
            % (jupyter_pid, uid, addresses, argv),
        )

    def test_documented_run_does_not_publish_every_interface(self):
        for path in (DOCKERFILE, DOCKERFILE_DEV):
            bindings = documented_port_bindings(path)
            for binding in bindings:
                if binding["container_port"] != str(NOTEBOOK_PORT):
                    continue
                with self.subTest(dockerfile=os.path.basename(path), spec=binding["raw"]):
                    self.assertTrue(
                        binding["host_ip"] and _loopback(binding["host_ip"]),
                        "%s documents %r, which publishes container port %s on %s"
                        % (
                            os.path.basename(path),
                            binding["raw"],
                            binding["container_port"],
                            binding["host_ip"] or "0.0.0.0",
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
