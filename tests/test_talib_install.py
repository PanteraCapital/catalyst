"""
Tests for the scripts that download, build and install the TA-Lib C library.

The download tools (curl/wget) are replaced with a stub that serves whatever
archive the test queues up, standing in for anyone able to alter the bytes on
the wire. The real bash, tar and sha256sum are used, and the stubbed
``configure``/``Makefile`` inside the served archive record whether they were
ever executed.
"""
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
INSTALLER = os.path.join(REPO_ROOT, 'etc', 'install_talib.sh')
CONDA_BUILD_SH = os.path.join(REPO_ROOT, 'conda', 'ta-lib', 'build.sh')
CONDA_BLD_BAT = os.path.join(REPO_ROOT, 'conda', 'ta-lib', 'bld.bat')

TALIB_SRC_SHA256 = (
    '9ff41efcb1c011a4b4b6dfc91610b06e39b1d7973ed5d4dee55029a0ac4dc651'
)
TALIB_MSVC_SHA256 = (
    '2dae7a1391222f0a7e601ecda654f93cebedfdb37afeb30df2721a3b0a717875'
)

# Files that fetch a TA-Lib C archive, and the digest each one must pin.
TALIB_FETCHERS = {
    '.travis.yml': None,
    'vagrant_init.sh': None,
    'Dockerfile': None,
    os.path.join('etc', 'install_talib.sh'): TALIB_SRC_SHA256,
    os.path.join('conda', 'ta-lib', 'build.sh'): TALIB_SRC_SHA256,
    os.path.join('conda', 'ta-lib', 'bld.bat'): TALIB_MSVC_SHA256,
}

TALIB_ARCHIVE_URL = re.compile(
    r'[a-z]+://\S*ta-lib-0\.4\.0-(?:src\.tar\.gz|msvc\.zip)\S*',
    re.IGNORECASE,
)

FETCH_STUB = r'''#!{python}
import os
import shutil
import sys

queue_dir = {queue_dir!r}
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
url = None
output = None
i = 0
while i < len(args):
    arg = args[i]
    if arg in ('-o', '--output', '-O', '--output-document'):
        output = args[i + 1]
        i += 1
    elif arg.startswith('http://') or arg.startswith('https://'):
        url = arg
    i += 1

with open(os.path.join(queue_dir, 'requests.log'), 'a') as log:
    log.write('%s %s\n' % (tool, url))

counter = os.path.join(queue_dir, 'counter')
n = int(open(counter).read()) if os.path.exists(counter) else 0
with open(counter, 'w') as f:
    f.write(str(n + 1))
payloads = sorted(p for p in os.listdir(queue_dir) if p.startswith('payload-'))
payload = os.path.join(queue_dir, payloads[min(n, len(payloads) - 1)])

if output is None and tool == 'wget':
    output = url.rstrip('/').split('/')[-1]
if output is None or output == '-':
    with open(payload, 'rb') as src:
        shutil.copyfileobj(src, getattr(sys.stdout, 'buffer', sys.stdout))
else:
    shutil.copyfile(payload, output)
'''

SUDO_STUB = r'''#!/bin/sh
echo "$*" >> {log!r}
exec "$@"
'''


def _write_executable(path, contents):
    with open(path, 'w') as f:
        f.write(contents)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)


def make_source_archive(label, marker):
    """A ta-lib source tarball whose build steps append ``label`` to
    ``marker`` instead of compiling anything."""
    configure = (
        '#!/bin/sh\n'
        'echo "{label} configure uid=$(id -u)" >> "{marker}"\n'
        'printf \'all:\\n\\techo "{label} make uid=$$(id -u)" >> "{marker}"'
        '\\ninstall:\\n\\techo "{label} install uid=$$(id -u)" >> "{marker}"'
        '\\n\' > Makefile\n'
    ).format(label=label, marker=marker)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        data = configure.encode('ascii')
        info = tarfile.TarInfo('ta-lib/configure')
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TalibInstallTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.queue_dir = os.path.join(self.tmp, 'served')
        self.bin_dir = os.path.join(self.tmp, 'bin')
        self.workdir = os.path.join(self.tmp, 'checkout')
        for d in (self.queue_dir, self.bin_dir, self.workdir):
            os.mkdir(d)
        shutil.copytree(os.path.join(REPO_ROOT, 'etc'),
                        os.path.join(self.workdir, 'etc'))

        self.marker = os.path.join(self.tmp, 'executed.log')
        self.sudo_log = os.path.join(self.tmp, 'sudo.log')

        fetch = FETCH_STUB.format(python=sys.executable,
                                  queue_dir=self.queue_dir)
        for tool in ('curl', 'wget'):
            _write_executable(os.path.join(self.bin_dir, tool), fetch)
        _write_executable(os.path.join(self.bin_dir, 'sudo'),
                          SUDO_STUB.format(log=self.sudo_log))

        self.env = dict(os.environ)
        self.env['PATH'] = self.bin_dir + os.pathsep + self.env['PATH']

    def serve(self, *archives):
        for i, data in enumerate(archives):
            name = 'payload-%03d' % i
            with open(os.path.join(self.queue_dir, name), 'wb') as f:
                f.write(data)

    def run_bash(self, script, env=None):
        proc = subprocess.Popen(
            ['bash', '-c', script],
            cwd=self.workdir,
            env=env or self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out = proc.communicate()[0].decode('utf-8', 'replace')
        return proc.returncode, out

    def executed(self):
        if not os.path.exists(self.marker):
            return []
        with open(self.marker) as f:
            return f.read().splitlines()

    def requests(self):
        path = os.path.join(self.queue_dir, 'requests.log')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def travis_talib_steps(self):
        with open(os.path.join(REPO_ROOT, '.travis.yml')) as f:
            config = yaml.safe_load(f)
        steps = [cmd for cmd in config['before_install']
                 if re.search(r'ta-?lib', cmd, re.IGNORECASE)]
        self.assertTrue(steps, 'no TA-Lib step found in before_install')
        return steps

    def test_travis_before_install_rejects_substituted_archive(self):
        self.serve(make_source_archive('attacker', self.marker))

        script = 'set -e\n' + '\n'.join(self.travis_talib_steps())
        status, out = self.run_bash(script)

        self.assertEqual(self.executed(), [], out)
        self.assertNotEqual(status, 0, out)

    def test_installer_rejects_substituted_archive(self):
        self.assertTrue(os.path.isfile(INSTALLER), INSTALLER)
        self.serve(make_source_archive('attacker', self.marker))
        prefix = os.path.join(self.tmp, 'prefix')
        os.mkdir(prefix)

        status, out = self.run_bash('bash %s %s' % (INSTALLER, prefix))

        self.assertEqual(self.executed(), [], out)
        self.assertNotEqual(status, 0, out)
        self.assertEqual(os.listdir(prefix), [])
        self.assertFalse(os.path.exists(os.path.join(self.workdir, 'ta-lib')))

    def test_installer_builds_only_the_verified_download(self):
        trusted = make_source_archive('trusted', self.marker)
        attacker = make_source_archive('attacker', self.marker)
        # Any download after the first one gets the attacker's archive.
        self.serve(trusted, attacker)

        with open(INSTALLER) as f:
            source = f.read()
        self.assertEqual(source.count(TALIB_SRC_SHA256), 1)
        installer = os.path.join(self.tmp, 'install_talib.sh')
        with open(installer, 'w') as f:
            f.write(source.replace(TALIB_SRC_SHA256,
                                   hashlib.sha256(trusted).hexdigest()))

        prefix = os.path.join(self.tmp, 'prefix')
        os.mkdir(prefix)
        os.chmod(prefix, 0o555)
        self.addCleanup(os.chmod, prefix, 0o755)

        status, out = self.run_bash('bash %s %s' % (installer, prefix))

        self.assertEqual(status, 0, out)
        uid = os.getuid()
        self.assertEqual(self.executed(), [
            'trusted configure uid=%d' % uid,
            'trusted make uid=%d' % uid,
            'trusted install uid=%d' % uid,
        ], out)
        self.assertEqual(len(self.requests()), 1, self.requests())
        if uid != 0:
            with open(self.sudo_log) as f:
                self.assertEqual(f.read().splitlines(), ['make install'])

    def test_conda_build_rejects_substituted_archive(self):
        self.serve(make_source_archive('attacker', self.marker))
        env = dict(self.env)
        env['PREFIX'] = os.path.join(self.tmp, 'conda-prefix')
        os.mkdir(env['PREFIX'])

        status, out = self.run_bash('bash %s' % CONDA_BUILD_SH, env=env)

        self.assertEqual(self.executed(), [], out)
        self.assertNotEqual(status, 0, out)

    def test_talib_archive_urls_use_https_and_are_pinned(self):
        for relpath, digest in sorted(TALIB_FETCHERS.items()):
            with open(os.path.join(REPO_ROOT, relpath)) as f:
                contents = f.read()
            for url in TALIB_ARCHIVE_URL.findall(contents):
                self.assertTrue(url.startswith('https://'),
                                '%s fetches %s' % (relpath, url))
                self.assertIsNotNone(
                    digest, '%s downloads TA-Lib itself: %s' % (relpath, url))
            if digest is not None:
                self.assertIn(digest, contents.lower(), relpath)

    def test_windows_build_checks_digest_before_extracting(self):
        with open(CONDA_BLD_BAT) as f:
            contents = f.read()
        lower = contents.lower()
        check = lower.find(TALIB_MSVC_SHA256)
        extract = lower.find('extracttodirectory')
        self.assertNotEqual(check, -1, 'msvc zip digest is not checked')
        self.assertNotEqual(extract, -1)
        self.assertLess(check, extract)
        self.assertEqual(lower.count('downloadfile('), 1)
        self.assertLess(lower.find('downloadfile('), check)


if __name__ == '__main__':
    unittest.main()
