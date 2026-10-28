"""Remote bundle tar extraction must not write outside its output directory.

The default ingest path downloads a tar from a fixed URL and extracts every
member. These tests are the spec for a fix: verify the downloaded bytes, and
refuse absolute paths, links, and member names that escape the output
directory. Refusal must not create, modify, or delete a file outside that
directory first, including a write that is rolled back afterward.

They fail while that behavior is still present. Run:

    python3 -m unittest tests.data.bundles.test_unsigned_bundle_extract -v
"""
from __future__ import absolute_import

import collections
import collections.abc
import ctypes
import importlib
import importlib.machinery
import inspect
import io
import os
import select
import shutil
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Python 3.10+ removed the collections aliases this tree imports at module
# scope. Restore them before importing catalyst. This does not change the
# extraction code under test.
for _name in (
    'Mapping', 'MutableMapping', 'Sequence', 'Iterable', 'Callable',
    'MutableSet', 'Set', 'Iterator', 'Container',
):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))

_ArgSpec = namedtuple('ArgSpec', 'args varargs keywords defaults')


def _getargspec(func):
    full = inspect.getfullargspec(func)
    return _ArgSpec(full.args, full.varargs, full.varkw, full.defaults)


inspect.getargspec = _getargspec


class _PatchSixFinder(object):
    """Let requests 2.18.4 import on Python 3.12.

    urllib3 1.22 vendors six 1.10. Its meta-path finder only implements
    find_module, which Python 3.12 no longer calls. The requests redirect
    logic itself is unchanged.
    """

    def find_spec(self, fullname, path, target=None):
        if not (
            fullname == 'urllib3.packages.six.moves' or
            fullname.startswith('urllib3.packages.six.moves.')
        ):
            return None
        if 'urllib3.packages.six' not in sys.modules:
            importlib.import_module('urllib3.packages.six')
        for importer in sys.meta_path:
            if (
                type(importer).__name__ == '_SixMetaPathImporter' and
                getattr(importer, 'name', None) == 'urllib3.packages.six'
            ):
                if not getattr(importer, '_py312_patched', False):
                    def find_spec(fullname, path=None, target=None,
                                  self=importer):
                        if fullname in self.known_modules:
                            return importlib.machinery.ModuleSpec(
                                fullname, self,
                            )
                        return None

                    def create_module(spec, self=importer):
                        return self.load_module(spec.name)

                    def exec_module(module):
                        return None

                    importer.find_spec = find_spec
                    importer.create_module = create_module
                    importer.exec_module = exec_module
                    importer._py312_patched = True
                return importer.find_spec(fullname, path, target)
        return None


sys.meta_path.insert(0, _PatchSixFinder())

import pandas as pd

_tslib = types.ModuleType('pandas.tslib')
_tslib.iNaT = pd.NaT.value
_tslib.normalize_date = (
    lambda ts: ts.normalize() if hasattr(ts, 'normalize') else ts
)
sys.modules['pandas.tslib'] = _tslib
_tools = types.ModuleType('pandas.tseries.tools')
_tools.to_datetime = pd.to_datetime
sys.modules['pandas.tseries.tools'] = _tools

if not hasattr(pd.DataFrame, 'to_msgpack'):
    def _to_msgpack(self, path_or_buf, *args, **kwargs):
        return None

    pd.DataFrame.to_msgpack = _to_msgpack
if not hasattr(pd, 'read_msgpack'):
    pd.read_msgpack = lambda *args, **kwargs: None

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'catalyst'))


def _shell(name, path):
    if name in sys.modules and hasattr(sys.modules[name], '__path__'):
        return sys.modules[name]
    module = types.ModuleType(name)
    module.__path__ = [path]
    module.__package__ = name
    module.__file__ = os.path.join(path, '__init__.py')
    sys.modules[name] = module
    return module


def _stub(name, **attrs):
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


# Import the bundle modules without executing catalyst/__init__.py, which
# pulls the whole application. The modules on the ingest path are the real
# source files. Extensions that are not on that path are stubbed only so
# those files can be imported on this interpreter.
_shell('catalyst', _ROOT)
for _pkg, _path in (
    ('catalyst.utils', os.path.join(_ROOT, 'utils')),
    ('catalyst.data', os.path.join(_ROOT, 'data')),
    ('catalyst.data.bundles', os.path.join(_ROOT, 'data', 'bundles')),
    ('catalyst.exchange', os.path.join(_ROOT, 'exchange')),
    ('catalyst.exchange.utils', os.path.join(_ROOT, 'exchange', 'utils')),
    ('catalyst.marketplace', os.path.join(_ROOT, 'marketplace')),
    ('catalyst.marketplace.utils', os.path.join(_ROOT, 'marketplace', 'utils')),
    ('catalyst.gens', os.path.join(_ROOT, 'gens')),
    ('catalyst.assets', os.path.join(_ROOT, 'assets')),
    ('catalyst.finance', os.path.join(_ROOT, 'finance')),
):
    _shell(_pkg, _path)


class _TradingPair(object):
    pass


class _LRU(dict):
    def __init__(self, size):
        super(_LRU, self).__init__()


class _AssetDBWriter(object):
    def __init__(self, *args, **kwargs):
        raise AssertionError('asset writers are not used for tar ingest')


class _Calendar(object):
    def __init__(self):
        self.first_session = pd.Timestamp('2014-01-02', tz='UTC')
        self.last_session = pd.Timestamp('2016-01-04', tz='UTC')


_assets = sys.modules['catalyst.assets']
_assets.AssetDBWriter = _AssetDBWriter
_assets.AssetFinder = type('AssetFinder', (), {})
_assets.ASSET_DB_VERSION = 6
_assets.TradingPair = _TradingPair
_stub(
    'catalyst.assets._assets',
    TradingPair=_TradingPair,
    Asset=object,
    Equity=object,
    Future=object,
    make_asset_array=lambda *a, **k: None,
    CACHE_FILE_TEMPLATE='',
)
_stub('catalyst.assets.asset_db_migrations', downgrade=lambda *a, **k: None)
_stub(
    'catalyst.data._equities',
    _compute_row_slices=lambda *a, **k: None,
    _read_bcolz_data=lambda *a, **k: None,
)
_stub(
    'catalyst.data._adjustments',
    load_adjustments_from_sqlite=lambda *a, **k: None,
)
_stub('catalyst.assets.continuous_futures', ContinuousFuture=object)
_stub(
    'catalyst.data._minute_bar_internal',
    minute_value=lambda *a, **k: None,
    find_position_of_minute=lambda *a, **k: None,
    find_last_traded_position_internal=lambda *a, **k: None,
)
# Value taken from catalyst/gens/sim_engine.pyx. Minute-bar code is not on
# the tar-extract path; the import only needs the constant to succeed.
_stub('catalyst.gens.sim_engine', NANOS_IN_MINUTE=60000000000)
_stub('bcolz', carray=object, ctable=object)
_stub('lru', LRU=_LRU)
_stub('tables')

_calendars = types.ModuleType('catalyst.utils.calendars')
_calendars.get_calendar = lambda name: _Calendar()
_calendars.register_calendar_alias = lambda *a, **k: None
_calendars.register_calendar = lambda *a, **k: None
sys.modules['catalyst.utils.calendars'] = _calendars

import catalyst.utils.preprocess as _preprocess

_preprocess._code_argorder = (
    'co_argcount',
    'co_posonlyargcount',
    'co_kwonlyargcount',
    'co_nlocals',
    'co_stacksize',
    'co_flags',
    'co_code',
    'co_consts',
    'co_names',
    'co_varnames',
    'co_filename',
    'co_name',
    'co_qualname',
    'co_firstlineno',
    'co_linetable',
    'co_exceptiontable',
    'co_freevars',
    'co_cellvars',
)

import catalyst.data.bundles.core as bundles_core
import catalyst.data.bundles.poloniex as poloniex_bundle
import catalyst.data.bundles.quandl as quandl_bundle
from catalyst.exchange.utils.bundle_utils import get_bcolz_chunk
from catalyst.exchange.utils.exchange_utils import get_exchange_bundles_folder
from catalyst.marketplace.utils.path_utils import extract_bundle
import catalyst.utils.paths as pth

# run_algo.py imports the trading stack at module scope. Those imports are
# not on the extension-execution path. Stub them so the real load_extensions
# function can be defined and called.
_stub('catalyst.data.data_portal', DataPortal=object)
_stub(
    'catalyst.exchange.exchange_pricing_loader',
    ExchangePricingLoader=object,
    TradingPairPricing=object,
)
_stub('catalyst.exchange.utils.factory', get_exchange=lambda *a, **k: None)
_stub('catalyst.finance.trading', TradingEnvironment=object)
_stub(
    'catalyst.utils.factory',
    create_simulation_parameters=lambda *a, **k: None,
)
_stub('catalyst.data.loader', load_crypto_market_data=lambda *a, **k: None)
_stub(
    'catalyst.exchange.exchange_algorithm',
    ExchangeTradingAlgorithmLive=object,
    ExchangeTradingAlgorithmBacktest=object,
)
_stub(
    'catalyst.exchange.exchange_data_portal',
    DataPortalExchangeLive=object,
    DataPortalExchangeBacktest=object,
)
_stub('catalyst.exchange.exchange_asset_finder', ExchangeAssetFinder=object)
sys.modules['catalyst.data.bundles'].load = bundles_core.load

from catalyst.utils.run_algo import load_extensions


POLO_URL = (
    'https://s3.amazonaws.com/enigmaco/catalyst-bundles/'
    'poloniex/poloniex-bundle.tar.gz'
)
QUANDL_URL = 'https://s3.amazonaws.com/quantopian-public-zipline-data/quandl'
BENIGN_EXTENSION = 'benign = True\n'
INGEST_TS = pd.Timestamp('2016-06-01', tz='UTC')

_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MODIFY = 0x00000002
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_WATCH_MASK = (
    _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_CREATE | _IN_DELETE | _IN_MODIFY |
    _IN_MOVED_FROM | _IN_MOVED_TO
)


class _InotifyEvent(ctypes.Structure):
    _fields_ = [
        ('wd', ctypes.c_int),
        ('mask', ctypes.c_uint32),
        ('cookie', ctypes.c_uint32),
        ('length', ctypes.c_uint32),
    ]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [
    ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32,
]
_libc.inotify_add_watch.restype = ctypes.c_int


class _DirectoryWatcher(object):
    """Record creates, writes, deletes, and renames in specific directories.

    A fix that writes an outside file and then removes it still fails.
    """

    def __init__(self, directories):
        self.events = []
        self._fd = _libc.inotify_init1(0)
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, 'inotify_init1 failed')
        self._names = {}
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
            watch = _libc.inotify_add_watch(
                self._fd, os.fsencode(directory), _WATCH_MASK,
            )
            if watch < 0:
                err = ctypes.get_errno()
                raise OSError(err, 'inotify_add_watch failed for %s' % directory)
            self._names[watch] = directory
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        poller = select.poll()
        poller.register(self._fd, select.POLLIN)
        while not self._stop.is_set():
            if not poller.poll(100):
                continue
            try:
                blob = os.read(self._fd, 4096)
            except OSError:
                return
            offset = 0
            header = ctypes.sizeof(_InotifyEvent)
            while offset + header <= len(blob):
                event = _InotifyEvent.from_buffer_copy(blob[offset:offset + header])
                name_end = offset + header + event.length
                raw = blob[offset + header:name_end].split(b'\x00', 1)[0]
                self.events.append((
                    self._names.get(event.wd),
                    os.fsdecode(raw),
                    int(event.mask),
                ))
                offset = name_end

    def close(self):
        if getattr(self, '_closed', False):
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self._fd)


class _TarHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/fetch' and self.server.redirect:
            location = 'http://127.0.0.1:%d/payload' % self.server.server_address[1]
            body = b''
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path in ('/fetch', '/payload'):
            body = self.server.tar_bytes
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        return


def _tar_bytes(members):
    """Build a gzip tar. members are (name, kind, payload) tuples.

    kind is 'file', 'symlink', or 'dir'. payload is bytes or a link target.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name=name)
            if kind == 'file':
                data = payload
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            elif kind == 'symlink':
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                archive.addfile(info)
            elif kind == 'dir':
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            else:
                raise AssertionError(kind)
    return buffer.getvalue()


def _escape_name(output_dir, target):
    relative = os.path.relpath(target, output_dir)
    if os.pardir not in relative.split(os.sep):
        raise AssertionError('target %s is inside %s' % (target, output_dir))
    return relative.replace(os.sep, '/')


class UnsignedBundleExtractTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='catalyst-tar-slip-')
        self.outside = os.path.join(self.root, 'outside')
        os.makedirs(self.outside)
        self.extension = os.path.join(self.root, 'extension.py')
        with open(self.extension, 'w') as handle:
            handle.write(BENIGN_EXTENSION)
        self.extension_inode = os.lstat(self.extension).st_ino
        self.sentinel = os.path.join(self.outside, 'executed.txt')
        self.environ = {
            'ZIPLINE_ROOT': self.root,
            'CATALYST_DOWNLOAD_ATTEMPTS': '1',
        }
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _TarHandler)
        self.server.tar_bytes = b''
        self.server.redirect = False
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        port = self.server.server_address[1]
        self._fetch_url = 'http://127.0.0.1:%d/fetch' % port
        self.seen_urls = []
        self._orig_get = bundles_core.requests.get

        def _routing_get(url, **kwargs):
            # Redirect targets are already on the loopback server. Every
            # other URL the product asks for is served from that server too,
            # so this test never contacts the published bundle hosts.
            if url.startswith('http://127.0.0.1:') or url.startswith('http://localhost:'):
                return self._orig_get(url, **kwargs)
            self.seen_urls.append(url)
            return self._orig_get(self._fetch_url, **kwargs)

        bundles_core.requests.get = _routing_get
        self.watcher = _DirectoryWatcher([self.root, self.outside])

    def tearDown(self):
        bundles_core.requests.get = self._orig_get
        self.watcher.close()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=2)
        shutil.rmtree(self.root, ignore_errors=True)

    def _serve(self, members, redirect):
        self.server.tar_bytes = _tar_bytes(members)
        self.server.redirect = redirect
        self.seen_urls = []

    def _poloniex_output(self):
        naive = INGEST_TS.tz_convert('UTC').tz_localize(None)
        timestr = bundles_core.to_bundle_ingest_dirname(naive)
        return pth.data_path(['poloniex', timestr], environ=self.environ)

    def _ingest_poloniex(self, show_progress):
        bundles_core.ingest(
            'poloniex',
            environ=self.environ,
            timestamp=INGEST_TS,
            show_progress=show_progress,
            is_compile=False,
        )

    def _ingest_registered(self, name, output_dir, show_progress):
        bundles_core.bundles[name].ingest(
            self.environ,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            {},
            show_progress,
            False,
            output_dir,
        )

    def _outside_events(self):
        flagged = []
        for directory, name, mask in self.watcher.events:
            if os.path.realpath(directory) == os.path.realpath(self.outside):
                flagged.append((directory, name, mask))
            elif name == 'extension.py':
                flagged.append((directory, name, mask))
        return flagged

    def _assert_contained(self, output_dir):
        self.watcher.close()
        self.assertFalse(
            os.path.exists(self.sentinel),
            'extension payload was executed',
        )
        with open(self.extension) as handle:
            extension = handle.read()
        self.assertEqual(extension, BENIGN_EXTENSION)
        self.assertEqual(os.lstat(self.extension).st_ino, self.extension_inode)
        leaked = []
        for dirpath, dirnames, filenames in os.walk(self.outside):
            for name in dirnames + filenames:
                leaked.append(os.path.join(dirpath, name))
        self.assertEqual(leaked, [])
        self.assertEqual(self._outside_events(), [])
        if output_dir and os.path.isdir(output_dir):
            real_out = os.path.realpath(output_dir)
            for dirpath, dirnames, filenames in os.walk(output_dir, followlinks=False):
                for name in dirnames + filenames:
                    path = os.path.join(dirpath, name)
                    if not os.path.islink(path):
                        continue
                    target = os.path.realpath(path)
                    try:
                        common = os.path.commonpath([target, real_out])
                    except ValueError:
                        common = None
                    self.assertEqual(
                        common,
                        real_out,
                        'link %s resolves to %s' % (path, target),
                    )

    def test_poloniex_ingest_refuses_parent_segments_without_progress(self):
        output_dir = self._poloniex_output()
        member = _escape_name(output_dir, self.extension)
        self._serve([
            (member, 'file', b'pwned = True\n'),
        ], redirect=False)
        self._ingest_poloniex(show_progress=False)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_poloniex_ingest_refuses_parent_segments_with_progress(self):
        output_dir = self._poloniex_output()
        member = _escape_name(output_dir, self.extension)
        self._serve([
            (member, 'file', b'pwned = True\n'),
        ], redirect=False)
        self._ingest_poloniex(show_progress=True)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_poloniex_ingest_refuses_parent_segments_reached_by_redirect(self):
        output_dir = self._poloniex_output()
        member = _escape_name(output_dir, self.extension)
        self._serve([
            (member, 'file', b'pwned = True\n'),
        ], redirect=True)
        self._ingest_poloniex(show_progress=True)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_poloniex_ingest_refuses_absolute_member_path(self):
        output_dir = self._poloniex_output()
        target = os.path.join(self.outside, 'absolute.py')
        self._serve([
            (target, 'file', b'absolute = True\n'),
        ], redirect=False)
        self._ingest_poloniex(show_progress=False)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_poloniex_ingest_refuses_nested_parent_segments(self):
        output_dir = self._poloniex_output()
        # A leading directory component hides the parent segments from a
        # check that only rejects names starting with '..'. The directory
        # member is extracted first so the later member's parent exists and
        # the write is not rejected by makedirs itself.
        member = 'nested/../' + _escape_name(output_dir, self.extension)
        self._serve([
            ('nested', 'dir', b''),
            (member, 'file', b'nested = True\n'),
        ], redirect=False)
        self._ingest_poloniex(show_progress=False)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_poloniex_ingest_refuses_symlink_write_through(self):
        output_dir = self._poloniex_output()
        self._serve([
            ('jump', 'symlink', self.outside),
            ('jump/owned.txt', 'file', b'through-link\n'),
        ], redirect=False)
        self._ingest_poloniex(show_progress=False)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_poloniex_ingest_refuses_relative_symlink_write_through(self):
        output_dir = self._poloniex_output()
        linkname = _escape_name(output_dir, self.outside)
        self._serve([
            ('jump', 'symlink', linkname),
            ('jump/owned.txt', 'file', b'through-relative-link\n'),
        ], redirect=False)
        self._ingest_poloniex(show_progress=True)
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)

    def test_quandl_ingest_refuses_parent_segments(self):
        output_dir = pth.data_path(
            ['quandl', '2016-06-01T00;00;00'], environ=self.environ,
        )
        os.makedirs(output_dir)
        member = _escape_name(output_dir, self.extension)
        self._serve([
            (member, 'file', b'quandl = True\n'),
        ], redirect=False)
        self._ingest_registered('quandl', output_dir, show_progress=False)
        self.assertEqual(self.seen_urls, [QUANDL_URL])
        self._assert_contained(output_dir)

    def test_quandl_ingest_refuses_symlink_write_after_redirect(self):
        output_dir = pth.data_path(
            ['quandl', '2016-06-01T00;00;00'], environ=self.environ,
        )
        os.makedirs(output_dir)
        self._serve([
            ('jump', 'symlink', self.outside),
            ('jump/owned.txt', 'file', b'quandl-link\n'),
        ], redirect=True)
        self._ingest_registered('quandl', output_dir, show_progress=True)
        self.assertEqual(self.seen_urls, [QUANDL_URL])
        self._assert_contained(output_dir)

    def test_exchange_bcolz_chunk_refuses_escape_and_symlink(self):
        previous = os.environ.get('ZIPLINE_ROOT')
        os.environ['ZIPLINE_ROOT'] = self.root
        try:
            exchange_name = 'bitfinex'
            symbol = 'btc_usdt'
            data_frequency = 'daily'
            period = '2017-10'
            name = '{exchange}-{frequency}-{symbol}-{period}'.format(
                exchange=exchange_name,
                frequency=data_frequency,
                symbol=symbol,
                period=period,
            )
            output_dir = os.path.join(
                get_exchange_bundles_folder(exchange_name, environ=self.environ),
                name,
            )
            expected_url = (
                'https://s3.amazonaws.com/enigmaco/catalyst-bundles/'
                'exchange-{exchange}/{name}.tar.gz'.format(
                    exchange=exchange_name,
                    name=name,
                )
            )
            member = _escape_name(output_dir, self.extension)
            # The chunk directory does not exist until extraction creates it.
            # An inside member has to land first; otherwise makedirs aborts
            # on the '..' member before the write.
            self._serve([
                ('inside.txt', 'file', b'inside\n'),
                (member, 'file', b'chunk = True\n'),
                ('jump', 'symlink', self.outside),
                ('jump/owned.txt', 'file', b'chunk-link\n'),
            ], redirect=True)
            try:
                get_bcolz_chunk(
                    exchange_name, symbol, data_frequency, period,
                )
            except Exception:
                pass
            self.assertEqual(self.seen_urls, [expected_url])
            self._assert_contained(output_dir)
        finally:
            if previous is None:
                os.environ.pop('ZIPLINE_ROOT', None)
            else:
                os.environ['ZIPLINE_ROOT'] = previous

    def test_marketplace_extract_bundle_refuses_escape_and_symlink(self):
        archive_path = os.path.join(self.root, 'market.tar.gz')
        output_dir = archive_path.replace('.tar.gz', '')
        member = _escape_name(output_dir, self.extension)
        with open(archive_path, 'wb') as handle:
            handle.write(_tar_bytes([
                ('inside.txt', 'file', b'inside\n'),
                (member, 'file', b'market = True\n'),
                (os.path.join(self.outside, 'absolute.bin'), 'file', b'abs'),
                ('jump', 'symlink', self.outside),
                ('jump/owned.txt', 'file', b'market-link\n'),
            ]))
        try:
            extract_bundle(archive_path)
        except Exception:
            pass
        self._assert_contained(output_dir)

    def test_overwritten_extension_is_not_executed(self):
        output_dir = self._poloniex_output()
        member = _escape_name(output_dir, self.extension)
        payload = (
            'open(%r, "w").write("executed\\n")\n' % self.sentinel
        ).encode('utf-8')
        self._serve([(member, 'file', payload)], redirect=True)
        self._ingest_poloniex(show_progress=True)
        try:
            load_extensions(
                True, (), True, self.environ, reload=True,
            )
        except Exception:
            pass
        self.assertEqual(self.seen_urls, [POLO_URL])
        self._assert_contained(output_dir)


if __name__ == '__main__':
    unittest.main()
