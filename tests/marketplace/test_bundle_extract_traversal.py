"""Confining tar extraction for marketplace and bundle archives.

These tests are the specification for the path-traversal bug in
``extractall`` call sites. They fail while member names can write outside
the destination directory, and they pass once extraction refuses those
members without creating them.

Run from the repository root:

    python3 -m unittest tests.marketplace.test_bundle_extract_traversal
"""
from __future__ import print_function

import collections
import collections.abc
import io
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# catalyst.utils.memoize imports collections.Sequence, which moved to
# collections.abc. Alias it before importing catalyst so the real modules
# load on this interpreter. The alias does not change path handling.
for _name in (
        'Sequence', 'Mapping', 'MutableMapping', 'Iterable', 'Callable'):
    if not hasattr(collections, _name) and hasattr(collections.abc, _name):
        setattr(collections, _name, getattr(collections.abc, _name))

import importlib.machinery
import importlib.util

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# catalyst/__init__.py imports the data and finance stacks before any
# marketplace module. Those imports are not on the extract path. Register
# the package tree here so the real extract modules load from their files.
_CATALYST_ROOT = os.path.join(REPO_ROOT, 'catalyst')


def _register_package(name, path):
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [path]
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition('.')
    if parent_name and parent_name in sys.modules:
        setattr(sys.modules[parent_name], child, module)
    return module


def _register_stub(name, **attrs):
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=False)
    module = importlib.util.module_from_spec(spec)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition('.')
    if parent_name and parent_name in sys.modules:
        setattr(sys.modules[parent_name], child, module)
    return module


def _install_import_stubs():
    if getattr(_install_import_stubs, 'done', False):
        return
    _register_package('catalyst', _CATALYST_ROOT)
    _register_package(
        'catalyst.utils', os.path.join(_CATALYST_ROOT, 'utils'))
    _register_package(
        'catalyst.data', os.path.join(_CATALYST_ROOT, 'data'))
    _register_package(
        'catalyst.data.bundles',
        os.path.join(_CATALYST_ROOT, 'data', 'bundles'))
    _register_package(
        'catalyst.marketplace', os.path.join(_CATALYST_ROOT, 'marketplace'))
    _register_package(
        'catalyst.marketplace.utils',
        os.path.join(_CATALYST_ROOT, 'marketplace', 'utils'))
    _register_package(
        'catalyst.exchange', os.path.join(_CATALYST_ROOT, 'exchange'))
    _register_package(
        'catalyst.exchange.utils',
        os.path.join(_CATALYST_ROOT, 'exchange', 'utils'))
    _register_package(
        'catalyst.finance', os.path.join(_CATALYST_ROOT, 'finance'))
    assets = _register_package(
        'catalyst.assets', os.path.join(_CATALYST_ROOT, 'assets'))
    assets.AssetDBWriter = type('AssetDBWriter', (), {})
    assets.AssetFinder = type('AssetFinder', (), {})
    assets.ASSET_DB_VERSION = 0
    _register_stub(
        'catalyst.data.us_equity_pricing',
        BcolzDailyBarReader=object,
        BcolzDailyBarWriter=object,
        SQLiteAdjustmentReader=object,
        SQLiteAdjustmentWriter=object,
    )
    _register_stub(
        'catalyst.data.minute_bars',
        BcolzMinuteBarReader=object,
        BcolzMinuteBarWriter=object,
    )
    _register_stub(
        'catalyst.assets.asset_db_migrations',
        downgrade=lambda *args, **kwargs: None,
    )
    _register_stub(
        'catalyst.utils.cache',
        dataframe_cache=None,
        working_dir=None,
        working_file=None,
    )
    _register_stub(
        'catalyst.utils.input_validation',
        ensure_timestamp=lambda value: value,
        optionally=lambda *args, **kwargs: (lambda fn: fn),
    )
    _register_stub(
        'catalyst.utils.preprocess',
        preprocess=lambda *args, **kwargs: (lambda fn: fn),
    )
    _register_stub(
        'catalyst.utils.calendars',
        get_calendar=lambda *args, **kwargs: None,
    )
    _register_stub(
        'catalyst.assets._assets',
        TradingPair=type('TradingPair', (), {}),
    )
    _register_stub('catalyst.data.data_portal', DataPortal=object)
    _register_stub(
        'catalyst.exchange.exchange_pricing_loader',
        ExchangePricingLoader=object,
        TradingPairPricing=object,
    )
    _register_stub(
        'catalyst.exchange.utils.factory',
        get_exchange=lambda *args, **kwargs: None,
    )
    _register_stub('catalyst.finance.trading', TradingEnvironment=object)
    _register_stub(
        'catalyst.utils.factory',
        create_simulation_parameters=lambda *args, **kwargs: None,
    )
    _register_stub(
        'catalyst.data.loader',
        load_crypto_market_data=lambda *args, **kwargs: None,
    )
    _register_stub(
        'catalyst.exchange.exchange_algorithm',
        ExchangeTradingAlgorithmLive=object,
        ExchangeTradingAlgorithmBacktest=object,
    )
    _register_stub(
        'catalyst.exchange.exchange_data_portal',
        DataPortalExchangeLive=object,
        DataPortalExchangeBacktest=object,
    )
    _register_stub(
        'catalyst.exchange.exchange_asset_finder',
        ExchangeAssetFinder=object,
    )
    bcolz = type(sys)('bcolz')
    bcolz.ctable = lambda *args, **kwargs: None
    sys.modules['bcolz'] = bcolz
    requests_toolbelt = type(sys)('requests_toolbelt')
    requests_toolbelt.MultipartDecoder = object
    sys.modules['requests_toolbelt'] = requests_toolbelt
    multipart = type(sys)('requests_toolbelt.multipart')
    sys.modules['requests_toolbelt.multipart'] = multipart
    decoder = type(sys)('requests_toolbelt.multipart.decoder')
    decoder.NonMultipartContentTypeException = type(
        'NonMultipartContentTypeException', (Exception,), {})
    sys.modules['requests_toolbelt.multipart.decoder'] = decoder
    _install_import_stubs.done = True


_install_import_stubs()

import catalyst.data.bundles.core as bundles_core  # noqa: E402

sys.modules['catalyst.data.bundles'].load = bundles_core.load

import catalyst.data.bundles.base as bundles_base  # noqa: E402
import catalyst.exchange.utils.bundle_utils as bundle_utils  # noqa: E402
import catalyst.marketplace.marketplace as marketplace_module  # noqa: E402
import catalyst.marketplace.utils.path_utils as path_utils  # noqa: E402
import catalyst.utils.run_algo as run_algo  # noqa: E402

from requests import Response  # noqa: E402

# inotify masks from /usr/include/linux/inotify.h and
# /usr/include/x86_64-linux-gnu/bits/inotify.h on this system.
_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_ISDIR = 0x40000000
_IN_NONBLOCK = 0o00004000
_IN_CLOEXEC = 0o02000000
_WRITE_EVENTS = _IN_CREATE | _IN_CLOSE_WRITE | _IN_MODIFY | _IN_MOVED_TO

import ctypes  # noqa: E402

_libc = ctypes.CDLL(None, use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [
    ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int


class _DirectoryWatch(object):
    """Record creates and writes under directories that must stay untouched."""

    def __init__(self, directories):
        flags = _IN_NONBLOCK | _IN_CLOEXEC
        self._fd = _libc.inotify_init1(flags)
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), 'inotify_init1')
        mask = _WRITE_EVENTS | _IN_DELETE | _IN_ISDIR
        self._watched = []
        for directory in directories:
            if not os.path.isdir(directory):
                continue
            watch = _libc.inotify_add_watch(
                self._fd, os.fsencode(directory), mask)
            if watch < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), directory)
            self._watched.append(directory)

    def names_written(self):
        written = []
        while True:
            try:
                raw = os.read(self._fd, 8192)
            except BlockingIOError:
                break
            if not raw:
                break
            offset = 0
            while offset + 16 <= len(raw):
                _wd, mask, _cookie, length = struct.unpack_from(
                    'iIII', raw, offset)
                offset += 16
                name = raw[offset:offset + length].split(b'\x00', 1)[0]
                offset += length
                if mask & _WRITE_EVENTS:
                    written.append(name.decode('utf-8', 'surrogateescape'))
        return written

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _member_name_outside(destination, target):
    """Tar member name that resolves to ``target`` from ``destination``."""
    relative = os.path.relpath(target, destination)
    return relative.replace(os.sep, '/')


def _add_regular(tar, name, payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(payload))


def _tar_bytes(populate):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        populate(tar)
    return buffer.getvalue()


def _write_tar(path, populate):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, mode='w:gz') as tar:
        populate(tar)


class _TarHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.body
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/gzip')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class _TarServer(object):
    def __init__(self, body):
        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), _TarHandler)
        self._httpd.body = body
        self.url = 'http://127.0.0.1:{0}/bundle.tar.gz'.format(
            self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


class BundleExtractTraversalTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='catalyst-tar-')
        self._old_root = os.environ.get('ZIPLINE_ROOT')
        os.environ['ZIPLINE_ROOT'] = self._tmpdir
        run_algo._loaded_extensions.clear()
        self.extension_path = os.path.join(self._tmpdir, 'extension.py')
        self.marker_path = os.path.join(self._tmpdir, 'extension-executed')

    def tearDown(self):
        if self._old_root is None:
            os.environ.pop('ZIPLINE_ROOT', None)
        else:
            os.environ['ZIPLINE_ROOT'] = self._old_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        run_algo._loaded_extensions.clear()

    def _safe_tar_path(self):
        # Same join ingest uses after a safe Content-Disposition filename.
        # catalyst/marketplace/marketplace.py reads the filename and passes
        # that path to process_temp_bundle.
        directory = os.path.join(
            self._tmpdir, 'data', 'marketplace', 'temp_bundles')
        os.makedirs(directory, exist_ok=True)
        header = 'attachment; filename="safe-bundle.tar.gz"'
        import re
        name = re.search(r'filename="(.*)"', header).group(1)
        return os.path.join(directory, name)

    def _destination_for(self, tar_path):
        return tar_path.replace('.tar.gz', '')

    def _assert_never_written(self, watch, paths):
        written = watch.names_written() if watch is not None else []
        for path in paths:
            name = os.path.basename(path)
            self.assertNotIn(
                name,
                written,
                '{0} was created during extraction'.format(path),
            )
            self.assertFalse(
                os.path.lexists(path),
                '{0} exists after extraction'.format(path),
            )

    def _watch(self, paths):
        directories = sorted(set(
            os.path.dirname(path) for path in paths if os.path.isdir(
                os.path.dirname(path))))
        return _DirectoryWatch(directories)

    def _payload(self):
        return (
            'open({0!r}, "w").write("executed")\n'.format(self.marker_path)
        ).encode('utf-8')

    def test_regular_member_is_extracted_inside_destination(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'kept')

        _write_tar(tar_path, populate)
        result = path_utils.extract_bundle(tar_path)
        self.assertEqual(os.path.realpath(result), os.path.realpath(destination))
        inside = os.path.join(destination, 'inside.txt')
        self.assertTrue(os.path.isfile(inside))
        with open(inside, 'rb') as handle:
            self.assertEqual(handle.read(), b'kept')

    def test_parent_directory_member_stays_inside_destination(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)
        # extract_bundle's destination is the tar path without the suffix, one
        # directory below temp_bundles. Three ".." segments leave that
        # directory and land in the data directory.
        escaped = os.path.join(self._tmpdir, 'data', 'extension.py')
        member = _member_name_outside(destination, escaped)
        self.assertIn('..', member.split('/'))

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'kept')
            _add_regular(tar, member, self._payload())

        _write_tar(tar_path, populate)
        watch = self._watch([escaped])
        try:
            try:
                path_utils.extract_bundle(tar_path)
            except Exception:
                pass
            self._assert_never_written(watch, [escaped])
        finally:
            watch.close()
        inside = os.path.join(destination, 'inside.txt')
        # A refusal may stop on the bad member after the safe one was written.
        if os.path.isfile(inside):
            with open(inside, 'rb') as handle:
                self.assertEqual(handle.read(), b'kept')

    def test_startup_extension_member_is_not_planted_or_executed(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)
        member = _member_name_outside(destination, self.extension_path)
        self.assertTrue(member.startswith('..'))

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'kept')
            _add_regular(tar, member, self._payload())

        _write_tar(tar_path, populate)
        watch = self._watch([self.extension_path])
        try:
            try:
                # self is unused. Calling the real method avoids Marketplace
                # construction, which prompts and contacts the network.
                marketplace_module.Marketplace.process_temp_bundle(
                    object(), 'sample-dataset', tar_path)
            except Exception:
                pass
            written_during_extract = watch.names_written()
        finally:
            watch.close()

        # load_extensions creates an empty extension.py when none was planted.
        # That create must not be attributed to the archive, so the watch is
        # already closed.
        run_algo.load_extensions(
            default=True,
            extensions=(),
            strict=True,
            environ=os.environ,
            reload=True,
        )
        self.assertFalse(
            os.path.exists(self.marker_path),
            'startup extension planted by the archive was executed',
        )
        self.assertNotIn(
            os.path.basename(self.extension_path),
            written_during_extract,
            'startup extension was created during extraction',
        )
        if os.path.isfile(self.extension_path):
            with open(self.extension_path, 'rb') as handle:
                self.assertNotIn(b'extension-executed', handle.read())

    def test_absolute_member_is_not_written(self):
        tar_path = self._safe_tar_path()
        absolute = os.path.join(self._tmpdir, 'absolute-outside', 'planted')
        os.makedirs(os.path.dirname(absolute))

        def populate(tar):
            _add_regular(tar, absolute, b'absolute')

        _write_tar(tar_path, populate)
        # The member name stored in the archive is an absolute path.
        with tarfile.open(tar_path, 'r') as tar:
            names = [member.name for member in tar.getmembers()]
        self.assertTrue(os.path.isabs(names[0]))

        watch = self._watch([absolute])
        try:
            try:
                path_utils.extract_bundle(tar_path)
            except Exception:
                pass
            self._assert_never_written(watch, [absolute])
        finally:
            watch.close()

    def test_symlink_member_does_not_redirect_writes(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)
        outside = os.path.join(self._tmpdir, 'symlink-outside')
        os.makedirs(os.path.dirname(outside), exist_ok=True)

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'kept')
            link = tarfile.TarInfo(name='redir')
            link.type = tarfile.SYMTYPE
            link.linkname = outside
            tar.addfile(link)
            _add_regular(tar, 'redir', b'through-link')

        _write_tar(tar_path, populate)
        watch = self._watch([outside])
        try:
            try:
                path_utils.extract_bundle(tar_path)
            except Exception:
                pass
            self._assert_never_written(watch, [outside])
        finally:
            watch.close()
        redir = os.path.join(destination, 'redir')
        if os.path.lexists(redir):
            self.assertFalse(
                os.path.islink(redir),
                'extraction created a symlink inside the destination',
            )

    def test_hardlink_member_name_does_not_escape(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)
        escaped = os.path.join(self._tmpdir, 'hardlink-escaped')

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'inside')
            link = tarfile.TarInfo(
                name=_member_name_outside(destination, escaped))
            link.type = tarfile.LNKTYPE
            link.linkname = 'inside.txt'
            tar.addfile(link)

        _write_tar(tar_path, populate)
        watch = self._watch([escaped])
        try:
            try:
                path_utils.extract_bundle(tar_path)
            except Exception:
                pass
            self._assert_never_written(watch, [escaped])
        finally:
            watch.close()

    def test_hardlink_to_absolute_target_is_not_created(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)
        secret = os.path.join(self._tmpdir, 'secret-target')
        with open(secret, 'wb') as handle:
            handle.write(b'secret')
        before_links = os.stat(secret).st_nlink

        def populate(tar):
            link = tarfile.TarInfo(name='alias')
            link.type = tarfile.LNKTYPE
            link.linkname = secret
            tar.addfile(link)

        _write_tar(tar_path, populate)
        try:
            path_utils.extract_bundle(tar_path)
        except Exception:
            pass
        alias = os.path.join(destination, 'alias')
        self.assertFalse(
            os.path.lexists(alias),
            'extraction created a hard link inside the destination',
        )
        self.assertEqual(os.stat(secret).st_nlink, before_links)
        with open(secret, 'rb') as handle:
            self.assertEqual(handle.read(), b'secret')

    def test_new_directory_outside_destination_is_not_created(self):
        tar_path = self._safe_tar_path()
        destination = self._destination_for(tar_path)
        planted = os.path.join(self._tmpdir, 'created-outside', 'planted')
        member = _member_name_outside(destination, planted)

        def populate(tar):
            _add_regular(tar, member, b'newdir')

        _write_tar(tar_path, populate)
        parent = os.path.dirname(planted)
        # Parent does not exist yet. Watch the directory that would gain it.
        watch = self._watch([os.path.join(self._tmpdir, 'created-outside')])
        try:
            try:
                path_utils.extract_bundle(tar_path)
            except Exception:
                pass
            written = watch.names_written()
            self.assertNotIn(os.path.basename(parent), written)
            self.assertFalse(os.path.lexists(planted))
            self.assertFalse(os.path.lexists(parent))
        finally:
            watch.close()

    def test_download_and_untar_confines_members(self):
        output_dir = os.path.join(self._tmpdir, 'bundle-output')
        os.makedirs(output_dir)
        escaped = os.path.join(self._tmpdir, 'untar-escaped')

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'kept')
            _add_regular(
                tar, _member_name_outside(output_dir, escaped), b'escaped')
            _add_regular(tar, os.path.join(self._tmpdir, 'untar-absolute'), b'abs')

        server = _TarServer(_tar_bytes(populate))
        bundle = type('Bundle', (), {})()
        bundle.tar_url = server.url
        watch = self._watch([
            escaped,
            os.path.join(self._tmpdir, 'untar-absolute'),
        ])
        try:
            try:
                bundles_base.BaseBundle._download_and_untar(
                    bundle, False, output_dir)
            except Exception:
                pass
            self._assert_never_written(watch, [
                escaped,
                os.path.join(self._tmpdir, 'untar-absolute'),
            ])
        finally:
            watch.close()
            server.close()

    def test_get_bcolz_chunk_confines_members(self):
        exchange_name = 'bitfinex'
        frequency = 'daily'
        symbol = 'neo_eth'
        period = '2017-10'
        # Mirror get_bcolz_chunk's destination so the member name escapes it.
        from catalyst.exchange.utils.exchange_utils import (
            get_exchange_bundles_folder,
        )
        root = get_exchange_bundles_folder(exchange_name)
        chunk_name = '{0}-{1}-{2}-{3}'.format(
            exchange_name, frequency, symbol, period)
        destination = os.path.join(root, chunk_name)
        escaped = os.path.join(self._tmpdir, 'chunk-escaped')

        def populate(tar):
            _add_regular(tar, 'inside.txt', b'kept')
            _add_regular(
                tar, _member_name_outside(destination, escaped), b'escaped')

        body = _tar_bytes(populate)
        response = Response()
        response.status_code = 200
        response.reason = 'OK'
        response.url = 'https://s3.amazonaws.com/enigmaco/catalyst-bundles/'
        response._content = body
        response._content_consumed = True

        def fake_get(url, *args, **kwargs):
            self.assertIn('enigmaco/catalyst-bundles/', url)
            response.url = url
            return response

        original_get = bundles_core.requests.get
        bundles_core.requests.get = fake_get
        watch = self._watch([escaped])
        try:
            try:
                bundle_utils.get_bcolz_chunk(
                    exchange_name, symbol, frequency, period)
            except Exception:
                pass
            self._assert_never_written(watch, [escaped])
        finally:
            bundles_core.requests.get = original_get
            watch.close()


if __name__ == '__main__':
    unittest.main()
