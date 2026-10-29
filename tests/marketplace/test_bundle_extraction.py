"""Downloaded bundle archives must not write outside their destination.

These tests drive the real ingest and download code paths with archives
whose member names, link targets or multipart filenames point outside the
directory the bundle is extracted into.

The full catalyst stack does not import on current interpreters, so the
package tree is registered by hand and modules unrelated to extraction are
stubbed. The modules under test load from their real source files.

    python3 -m unittest tests.marketplace.test_bundle_extraction -v
"""
from __future__ import print_function

import collections
import collections.abc
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

for _name in ('Sequence', 'Mapping', 'MutableMapping', 'Iterable',
              'Callable'):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CATALYST_ROOT = os.path.join(REPO_ROOT, 'catalyst')


def _register(name, path=None, **attrs):
    spec = importlib.machinery.ModuleSpec(
        name, loader=None, is_package=path is not None)
    if path is not None:
        spec.submodule_search_locations = [path]
    module = importlib.util.module_from_spec(spec)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, child = name.rpartition('.')
    if parent in sys.modules:
        setattr(sys.modules[parent], child, module)
    return module


def _noop(*args, **kwargs):
    return None


def _install_import_stubs():
    if 'catalyst' in sys.modules and getattr(
            sys.modules['catalyst'], '_extraction_test_stubs', False):
        return
    for name in ('catalyst', 'catalyst.utils', 'catalyst.data',
                 'catalyst.data.bundles', 'catalyst.marketplace',
                 'catalyst.marketplace.utils', 'catalyst.exchange',
                 'catalyst.exchange.utils', 'catalyst.finance',
                 'catalyst.assets'):
        _register(name, os.path.join(CATALYST_ROOT, *name.split('.')[1:]))
    sys.modules['catalyst']._extraction_test_stubs = True

    assets = sys.modules['catalyst.assets']
    assets.AssetDBWriter = assets.AssetFinder = object
    assets.ASSET_DB_VERSION = 0
    _register('catalyst.assets._assets', TradingPair=object)
    _register('catalyst.assets.asset_db_migrations', downgrade=_noop)
    _register('catalyst.data.us_equity_pricing',
              BcolzDailyBarReader=object, BcolzDailyBarWriter=object,
              SQLiteAdjustmentReader=object, SQLiteAdjustmentWriter=object)
    _register('catalyst.data.minute_bars',
              BcolzMinuteBarReader=object, BcolzMinuteBarWriter=object)
    _register('catalyst.data.data_portal', DataPortal=object)
    _register('catalyst.data.loader', load_crypto_market_data=_noop)
    _register('catalyst.utils.cache', dataframe_cache=None,
              working_dir=None, working_file=None)
    _register('catalyst.utils.input_validation',
              ensure_timestamp=lambda value: value,
              optionally=lambda *a, **k: (lambda fn: fn))
    _register('catalyst.utils.preprocess',
              preprocess=lambda *a, **k: (lambda fn: fn))
    _register('catalyst.utils.calendars', get_calendar=_noop)
    _register('catalyst.utils.factory', create_simulation_parameters=_noop)
    _register('catalyst.finance.trading', TradingEnvironment=object)
    _register('catalyst.exchange.exchange_pricing_loader',
              ExchangePricingLoader=object, TradingPairPricing=object)
    _register('catalyst.exchange.utils.factory', get_exchange=_noop)
    _register('catalyst.exchange.exchange_algorithm',
              ExchangeTradingAlgorithmLive=object,
              ExchangeTradingAlgorithmBacktest=object)
    _register('catalyst.exchange.exchange_data_portal',
              DataPortalExchangeLive=object,
              DataPortalExchangeBacktest=object)
    _register('catalyst.exchange.exchange_asset_finder',
              ExchangeAssetFinder=object)
    _register('bcolz', ctable=_noop)


_install_import_stubs()

import catalyst.data.bundles.core as bundles_core  # noqa: E402
sys.modules['catalyst.data.bundles'].load = bundles_core.load

import catalyst.data.bundles.base as bundles_base  # noqa: E402
import catalyst.exchange.utils.bundle_utils as bundle_utils  # noqa: E402
import catalyst.marketplace.marketplace as marketplace  # noqa: E402
import catalyst.marketplace.utils.path_utils as path_utils  # noqa: E402
import catalyst.utils.paths as catalyst_paths  # noqa: E402
import catalyst.utils.run_algo as run_algo  # noqa: E402

UnsafeArchiveError = getattr(
    catalyst_paths, 'UnsafeArchiveError',
    type('UnsafeArchiveError', (Exception,), {}))

EXTENSION_PAYLOAD = (
    "with open({marker!r}, 'w') as f:\n"
    "    f.write('executed')\n"
)


def _add_file(tar, name, payload=b'payload'):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def _add_link(tar, name, target, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    tar.addfile(info)


def _tar_bytes(populate):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        populate(tar)
    return buf.getvalue()


def _snapshot(root, exclude):
    """Every path under ``root`` that is not under ``exclude``."""
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if path == exclude or path.startswith(exclude + os.sep):
                continue
            seen.add(path)
    return seen


class _TarHandler(BaseHTTPRequestHandler):
    body = b''

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class BundleExtractionTestCase(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.base, 'catalyst_root')
        os.makedirs(self.root)
        self.environ = {'ZIPLINE_ROOT': self.root}
        patcher = mock.patch.dict(os.environ, self.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.marker = os.path.join(self.base, 'extension-ran')
        self.extension = os.path.join(self.root, 'extension.py')
        self.victim = os.path.join(self.base, 'victim.txt')
        with open(self.victim, 'w') as f:
            f.write('original')

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _payload(self):
        return EXTENSION_PAYLOAD.format(marker=self.marker).encode('utf-8')

    def _assert_startup_extension_untouched(self):
        run_algo.load_extensions(True, [], True, self.environ, reload=True)
        self.assertFalse(
            os.path.exists(self.marker),
            'startup extension written by the archive was executed')
        with open(self.extension, 'rb') as f:
            self.assertEqual(f.read(), b'')

    def _assert_victim_untouched(self):
        with open(self.victim) as f:
            self.assertEqual(f.read(), 'original')


class MarketplaceExtractionTests(BundleExtractionTestCase):
    def _temp_bundle(self, populate, name='safe-bundle.tar.gz'):
        path = os.path.join(path_utils.get_temp_bundles_folder(), name)
        with open(path, 'wb') as f:
            f.write(_tar_bytes(populate))
        return path

    def _process(self, path):
        marketplace.Marketplace.process_temp_bundle(object(), 'ds', path)

    def _run_ingest(self, parts):
        boundary = 'catalystboundary'
        body = b''
        for filename, content in parts:
            body += (
                '--{}\r\nContent-Disposition: attachment; '
                'filename="{}"\r\n\r\n'.format(boundary, filename)
            ).encode('utf-8') + content + b'\r\n'
        body += '--{}--\r\n'.format(boundary).encode('utf-8')
        response = mock.Mock(status_code=200, content=body, encoding=None,
                             headers={'content-type':
                                      'multipart/form-data; boundary=' +
                                      boundary})

        web3 = mock.Mock()
        web3.toHex.side_effect = lambda value: value
        mkt = object.__new__(marketplace.Marketplace)
        mkt.addresses = [{'pubAddr': '0xabc', 'key': 'k', 'secret': 's'}]
        mkt.mkt_contract = mock.Mock()
        fns = mkt.mkt_contract.functions
        fns.getDataProviderInfo.return_value.call.return_value = [
            None, None, None, None, True]
        fns.checkAddressSubscription.return_value.call.return_value = [
            '0xabc', 'ds', None, None, 0, True]
        mkt.choose_pubaddr = lambda: ('0xabc', 0)
        mkt.to_text = lambda value: value
        with mock.patch.object(marketplace, 'Web3', web3, create=True), \
                mock.patch.object(marketplace.requests, 'post',
                                  return_value=response):
            try:
                mkt.ingest('ds')
            except Exception as e:
                return e

    def test_regular_member_is_extracted_inside_destination(self):
        def populate(tar):
            _add_file(tar, 'col/data', b'bcolz')
            _add_file(tar, './meta', b'meta')

        target = path_utils.extract_bundle(self._temp_bundle(populate))
        with open(os.path.join(target, 'col', 'data'), 'rb') as f:
            self.assertEqual(f.read(), b'bcolz')
        with open(os.path.join(target, 'meta'), 'rb') as f:
            self.assertEqual(f.read(), b'meta')

    def test_ingest_member_cannot_plant_startup_extension(self):
        payload = self._payload()

        def populate(tar):
            _add_file(tar, 'col/data')
            _add_file(tar, '../../../../extension.py', payload)

        error = self._run_ingest([('safe-bundle.tar.gz',
                                   _tar_bytes(populate))])
        self._assert_startup_extension_untouched()
        self.assertIsInstance(error, UnsafeArchiveError)

    def test_ingest_part_filename_cannot_plant_startup_extension(self):
        error = self._run_ingest([('../../../extension.py', self._payload())])
        self._assert_startup_extension_untouched()
        self.assertIsNotNone(error)

    def test_process_temp_bundle_member_cannot_plant_startup_extension(self):
        payload = self._payload()

        def populate(tar):
            _add_file(tar, 'col/data')
            _add_file(tar, '../../../../extension.py', payload)

        path = self._temp_bundle(populate)
        with self.assertRaises(UnsafeArchiveError):
            try:
                self._process(path)
            finally:
                self._assert_startup_extension_untouched()

    def _assert_rejected_without_side_effects(self, populate):
        path = self._temp_bundle(populate)
        dest = path[:-len('.tar.gz')]
        before = _snapshot(self.base, dest)
        try:
            with self.assertRaises(UnsafeArchiveError):
                path_utils.extract_bundle(path)
        finally:
            self.assertEqual(_snapshot(self.base, dest), before,
                             'extraction wrote outside the destination')
            self._assert_victim_untouched()
        self.assertFalse(os.path.exists(dest),
                         'rejected archive was partially extracted')

    def test_parent_directory_member_is_rejected(self):
        def populate(tar):
            _add_file(tar, 'col/data')
            _add_file(tar, '../../../escaped.txt')

        self._assert_rejected_without_side_effects(populate)

    def test_absolute_member_is_rejected(self):
        target = os.path.join(self.base, 'absolute.txt')
        self._assert_rejected_without_side_effects(
            lambda tar: _add_file(tar, target))

    def test_new_directory_outside_destination_is_rejected(self):
        def populate(tar):
            _add_file(tar, 'col/data')
            info = tarfile.TarInfo('../../../newdir')
            info.type = tarfile.DIRTYPE
            tar.addfile(info)

        self._assert_rejected_without_side_effects(populate)

    def test_symlink_member_is_rejected(self):
        def populate(tar):
            _add_link(tar, 'link', self.base, tarfile.SYMTYPE)
            _add_file(tar, 'link/through-symlink.txt')

        self._assert_rejected_without_side_effects(populate)

    def test_hardlink_to_outside_file_is_rejected(self):
        def populate(tar):
            _add_link(tar, 'hl', '../../../../../victim.txt', tarfile.LNKTYPE)
            _add_file(tar, 'hl', b'overwritten')

        self._assert_rejected_without_side_effects(populate)

    def test_hardlink_to_absolute_target_is_rejected(self):
        def populate(tar):
            _add_link(tar, 'hl', self.victim, tarfile.LNKTYPE)
            _add_file(tar, 'hl', b'overwritten')

        self._assert_rejected_without_side_effects(populate)

    def test_preexisting_symlink_in_destination_is_not_followed(self):
        def populate(tar):
            _add_file(tar, 'redirect/escaped.txt')

        path = self._temp_bundle(populate)
        dest = path[:-len('.tar.gz')]
        os.makedirs(dest)
        os.symlink(self.base, os.path.join(dest, 'redirect'))
        with self.assertRaises(UnsafeArchiveError):
            path_utils.extract_bundle(path)
        self.assertFalse(
            os.path.exists(os.path.join(self.base, 'escaped.txt')))


class DownloadExtractionTests(BundleExtractionTestCase):
    def _escaping_tar(self):
        payload = self._payload()

        def populate(tar):
            _add_file(tar, 'daily/data')
            _add_file(tar, '../../extension.py', payload)

        return _tar_bytes(populate)

    def test_download_and_untar_confines_members(self):
        handler = type('Handler', (_TarHandler,),
                       {'body': self._escaping_tar()})
        server = HTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        bundle = mock.Mock(
            tar_url='http://127.0.0.1:{}/b.tar.gz'.format(
                server.server_address[1]))
        bundle.name = 'test'
        output_dir = os.path.join(self.root, 'data', 'test')
        os.makedirs(output_dir)
        try:
            with self.assertRaises(UnsafeArchiveError):
                bundles_base.BaseBundle._download_and_untar(
                    bundle, False, output_dir)
        finally:
            self._assert_startup_extension_untouched()

    def test_get_bcolz_chunk_confines_members(self):
        response = mock.Mock()
        folder = bundle_utils.get_exchange_bundles_folder('poloniex')
        name = 'poloniex-daily-eth_btc-2017'
        payload = self._payload()

        def populate(tar):
            _add_file(tar, 'daily/data')
            _add_file(tar, '../../../../../extension.py', payload)

        response.content = _tar_bytes(populate)
        with mock.patch.object(bundles_core.requests, 'get',
                               return_value=response):
            try:
                with self.assertRaises(UnsafeArchiveError):
                    bundle_utils.get_bcolz_chunk(
                        'poloniex', 'eth_btc', 'daily', '2017')
            finally:
                self._assert_startup_extension_untouched()
        self.assertFalse(os.path.isdir(os.path.join(folder, name)))


if __name__ == '__main__':
    unittest.main()
