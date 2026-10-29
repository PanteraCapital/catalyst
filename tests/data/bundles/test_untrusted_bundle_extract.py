import io
import os
import shutil
import tarfile
import tempfile
import threading
from unittest import TestCase

import bcolz
import mock
import numpy as np
import pandas as pd
import requests
from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

from catalyst.data.bundles import core as bundles_core
from catalyst.data.bundles.poloniex import PoloniexBundle
from catalyst.exchange.utils.bundle_utils import get_bcolz_chunk
from catalyst.marketplace.utils.path_utils import (
    extract_bundle,
    get_temp_bundles_folder,
)
from catalyst.utils.run_algo import load_extensions
import catalyst.utils.paths as pth

ORIGINAL_EXTENSION = b'# user extension\n'
_real_get = requests.get


class _Handler(BaseHTTPRequestHandler):
    files = {}

    def do_GET(self):
        if self.path.startswith('/redirect/'):
            self.send_response(302)
            self.send_header('Location', self.path[len('/redirect'):])
            self.end_headers()
            return
        body = self.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body, content_type = self.files[self.path]
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _add_file(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_link(tar, name, target, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    tar.addfile(info)


def _add_tree(tar, root, arcroot):
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            with open(full, 'rb') as f:
                _add_file(tar, arcroot + '/' + rel, f.read())


def _tar(build):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        build(tar)
    return buf.getvalue()


class _Payload(object):
    """Unpickling creates ``marker``."""
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return os.mkdir, (self.marker,)


class UntrustedBundleExtractTestCase(TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), _Handler)
        cls.base_url = 'http://127.0.0.1:{}'.format(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, 'catalyst_root')
        self.outside = os.path.join(self.tmp, 'outside')
        os.makedirs(self.root)
        os.makedirs(self.outside)
        self.victim = os.path.join(self.outside, 'victim.txt')
        with open(self.victim, 'wb') as f:
            f.write(b'original')
        self.environ = {'ZIPLINE_ROOT': self.root}
        self.extension = pth.default_extension(environ=self.environ)
        with open(self.extension, 'wb') as f:
            f.write(ORIGINAL_EXTENSION)
        self.marker = os.path.join(self.tmp, 'payload-ran')
        _Handler.files = {}

        patcher = mock.patch.dict(os.environ, self.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            bundles_core.requests, 'get', side_effect=self._get,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, url, *args, **kwargs):
        """Send every bundle download to the local server through a 302,
        keeping the URL path, so the real ``requests`` follows the redirect.
        """
        path = '/' + url.split('://', 1)[1].split('/', 1)[1]
        return _real_get(self.base_url + '/redirect' + path, *args, **kwargs)

    def _serve(self, path, data):
        _Handler.files[path] = data

    def _payload_ext(self):
        return ('open({!r}, "w").close()\n'.format(self.marker)).encode()

    def _escape_member(self, dest_dir, target):
        return os.path.relpath(target, dest_dir).replace(os.sep, '/')

    def _assert_nothing_escaped(self):
        with open(self.extension, 'rb') as f:
            self.assertEqual(
                f.read(), ORIGINAL_EXTENSION,
                'extension.py was overwritten from the bundle',
            )
        with open(self.victim, 'rb') as f:
            self.assertEqual(
                f.read(), b'original',
                'a file outside the destination was modified',
            )
        self.assertEqual(
            sorted(os.listdir(self.outside)), ['victim.txt'],
            'a file was created outside the destination',
        )
        self.assertFalse(os.path.exists(self.marker), 'payload was executed')

    def _malicious_archives(self, dest_dir):
        """Archives that write outside ``dest_dir`` when naively extracted."""
        ext_rel = self._escape_member(dest_dir, self.extension)
        outside_rel = self._escape_member(dest_dir, self.outside)
        payload = self._payload_ext()

        # A benign first member creates ``dest_dir`` when it does not exist
        # yet, as for exchange chunks and marketplace bundles.
        def parent(tar):
            _add_file(tar, 'ok.txt', b'ok')
            _add_file(tar, ext_rel, payload)

        def nested(tar):
            _add_file(tar, 'nested/ok.txt', b'ok')
            _add_file(tar, 'nested/../' + ext_rel, payload)

        def absolute(tar):
            _add_file(tar, 'ok.txt', b'ok')
            _add_file(tar, os.path.join(self.outside, 'abs.txt'), b'x')

        def abs_symlink(tar):
            _add_file(tar, 'ok.txt', b'ok')
            _add_link(tar, 'link', self.outside)
            _add_file(tar, 'link/evil.txt', b'x')

        def rel_symlink(tar):
            _add_file(tar, 'ok.txt', b'ok')
            _add_link(tar, 'rel', outside_rel)
            _add_file(tar, 'rel/evil.txt', b'x')

        def hardlink(tar):
            _add_file(tar, 'ok.txt', b'ok')
            _add_link(tar, 'hl', self.victim, kind=tarfile.LNKTYPE)
            _add_file(tar, 'hl', b'pwned')

        return [
            ('parent', parent),
            ('nested', nested),
            ('absolute', absolute),
            ('abs_symlink', abs_symlink),
            ('rel_symlink', rel_symlink),
            ('hardlink', hardlink),
        ]

    def _run_and_load_extensions(self, action):
        raised = None
        try:
            action()
        except ValueError as e:
            raised = e
        load_extensions(True, (), False, self.environ, reload=True)
        self._assert_nothing_escaped()
        return raised

    def test_poloniex_download_and_untar(self):
        bundle = PoloniexBundle()
        dest = pth.data_path(['poloniex', 'ingest'], environ=self.environ)
        os.makedirs(dest)
        path = '/enigmaco/catalyst-bundles/poloniex/poloniex-bundle.tar.gz'
        for show_progress in (True, False):
            for name, build in self._malicious_archives(dest):
                self._serve(path, _tar(build))
                raised = self._run_and_load_extensions(
                    lambda: bundle._download_and_untar(show_progress, dest)
                )
                self.assertIsNotNone(raised, name)
                self.assertEqual(os.listdir(dest), [], name)

    def test_poloniex_ingest_swallows_but_writes_nothing(self):
        bundle = PoloniexBundle()
        dest = pth.data_path(['poloniex', 'ingest'], environ=self.environ)
        os.makedirs(dest)
        path = '/enigmaco/catalyst-bundles/poloniex/poloniex-bundle.tar.gz'
        self._serve(path, _tar(self._malicious_archives(dest)[0][1]))
        self._run_and_load_extensions(lambda: bundle.ingest(
            self.environ, None, None, None, None, None, None, None, None,
            False, False, dest,
        ))

    def test_registered_quandl_ingest(self):
        ts = pd.Timestamp('2018-01-02', tz='utc')
        dest = pth.data_path(
            ['quandl', bundles_core.to_bundle_ingest_dirname(
                ts.tz_localize(None))],
            environ=self.environ,
        )
        self._serve(
            '/quantopian-public-zipline-data/quandl',
            _tar(self._malicious_archives(dest)[0][1]),
        )
        self._run_and_load_extensions(lambda: bundles_core.ingest(
            'quandl', environ=self.environ, timestamp=ts,
            show_progress=False, is_compile=False,
        ))

    def test_get_bcolz_chunk(self):
        folder = os.path.join(
            pth.data_root(self.environ), 'exchanges', 'bitfinex',
            'temp_bundles',
        )
        name = 'bitfinex-daily-btc_usdt-2017-10'
        dest = os.path.join(folder, name)
        url_path = ('/enigmaco/catalyst-bundles/exchange-bitfinex/'
                    '{}.tar.gz'.format(name))
        for label, build in self._malicious_archives(dest):
            shutil.rmtree(dest, ignore_errors=True)
            self._serve(url_path, _tar(build))
            raised = self._run_and_load_extensions(lambda: get_bcolz_chunk(
                'bitfinex', 'btc_usdt', 'daily', '2017-10',
            ))
            self.assertIsNotNone(raised, label)

    def test_marketplace_extract_bundle(self):
        folder = get_temp_bundles_folder(self.environ)
        tar_path = os.path.join(folder, 'ds.tar.gz')
        dest = tar_path.replace('.tar.gz', '')
        for label, build in self._malicious_archives(dest):
            shutil.rmtree(dest, ignore_errors=True)
            with open(tar_path, 'wb') as f:
                f.write(_tar(build))
            raised = self._run_and_load_extensions(
                lambda: extract_bundle(tar_path)
            )
            self.assertIsNotNone(raised, label)

    def test_marketplace_ingest_filename(self):
        from catalyst.marketplace import marketplace as mkt
        from requests_toolbelt import MultipartEncoder
        from web3 import Web3

        target = get_temp_bundles_folder(self.environ)
        name = self._escape_member(target, self.extension)
        encoder = MultipartEncoder(fields={
            'file': (name, self._payload_ext(), 'application/octet-stream'),
        })
        self._serve('/marketplace/ingest',
                    (encoder.to_string(), encoder.content_type))

        market = mkt.Marketplace.__new__(mkt.Marketplace)
        market.addresses = [{'pubAddr': '0xabc', 'key': 'k', 'secret': 's'}]
        market.choose_pubaddr = lambda: ('0xabc', 0)
        market.to_text = lambda value: value
        fns = market.mkt_contract = mock.MagicMock()
        fns = fns.functions
        fns.getDataProviderInfo.return_value.call.return_value = \
            [None] * 4 + [True]
        fns.checkAddressSubscription.return_value.call.return_value = \
            ['0xabc', 'ds', None, None, None, True]

        with mock.patch.object(mkt, 'Web3', Web3, create=True), \
                mock.patch.object(mkt, 'AUTH_SERVER', self.base_url), \
                mock.patch.object(mkt, 'get_signed_headers',
                                  return_value={}), \
                mock.patch.object(mkt.Marketplace, 'process_temp_bundle'):
            raised = self._run_and_load_extensions(
                lambda: market.ingest('ds')
            )
        self.assertIsNotNone(raised)

    def _object_column(self, root):
        os.makedirs(os.path.dirname(root))
        bcolz.carray(
            np.array([_Payload(self.marker)], dtype=object),
            rootdir=root, mode='w',
        ).flush()

    def test_object_dtype_column_is_refused(self):
        src = os.path.join(self.tmp, 'src', 'close')
        self._object_column(src)

        def build(tar):
            _add_tree(tar, src, 'daily_equities.bcolz/close')

        bundle = PoloniexBundle()
        dest = pth.data_path(['poloniex', 'ingest'], environ=self.environ)
        os.makedirs(dest)
        self._serve(
            '/enigmaco/catalyst-bundles/poloniex/poloniex-bundle.tar.gz',
            _tar(build),
        )
        raised = None
        try:
            bundle._download_and_untar(False, dest)
        except ValueError as e:
            raised = e
        column = os.path.join(dest, 'daily_equities.bcolz', 'close')
        if os.path.isdir(column):
            bcolz.open(rootdir=column, mode='r')[:]
        self.assertFalse(os.path.exists(self.marker),
                         'object column from the bundle was unpickled')
        self.assertIsNotNone(raised)

    def test_object_dtype_storage_name_variants_are_refused(self):
        from catalyst.utils.tar_utils import UnsafeArchiveError, safe_extract

        src = os.path.join(self.tmp, 'src', 'close')
        self._object_column(src)
        with open(os.path.join(src, 'meta', 'storage'), 'rb') as f:
            storage = f.read()
        dest = os.path.join(self.tmp, 'dest')
        for name in ('close/META/STORAGE', 'close/meta/storage.',
                     u'close/meta/\u017ftorage'):
            data = _tar(lambda tar: _add_file(tar, name, storage))
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                with self.assertRaises(UnsafeArchiveError):
                    safe_extract(tar, dest)
            self.assertFalse(os.path.exists(dest), name)

    def test_nothing_written_when_a_later_member_is_unsafe(self):
        from catalyst.utils.tar_utils import UnsafeArchiveError, safe_extract

        dest = os.path.join(self.tmp, 'dest')

        def build(tar):
            _add_file(tar, 'ok.txt', b'ok')
            _add_link(tar, 'link', self.outside)

        with tarfile.open(fileobj=io.BytesIO(_tar(build))) as tar:
            with self.assertRaises(UnsafeArchiveError):
                safe_extract(tar, dest)
        self.assertFalse(os.path.exists(dest))

    def test_valid_bundle_extracts(self):
        src = os.path.join(self.tmp, 'src', 'close')
        os.makedirs(os.path.dirname(src))
        bcolz.carray(np.arange(5, dtype='float64'), rootdir=src,
                     mode='w').flush()

        def build(tar):
            dot = tarfile.TarInfo('.')
            dot.type = tarfile.DIRTYPE
            dot.mode = 0o755
            tar.addfile(dot)
            _add_tree(tar, src, './daily_equities.bcolz/close')
            _add_file(tar, './metadata.json', b'{}')

        bundle = PoloniexBundle()
        dest = pth.data_path(['poloniex', 'ingest'], environ=self.environ)
        os.makedirs(dest)
        self._serve(
            '/enigmaco/catalyst-bundles/poloniex/poloniex-bundle.tar.gz',
            _tar(build),
        )
        bundle._download_and_untar(True, dest)
        column = bcolz.open(
            rootdir=os.path.join(dest, 'daily_equities.bcolz', 'close'),
            mode='r',
        )
        np.testing.assert_array_equal(column[:], np.arange(5.0))
        with open(os.path.join(dest, 'metadata.json'), 'rb') as f:
            self.assertEqual(f.read(), b'{}')
        self._assert_nothing_escaped()
