"""
Dataset names come from the marketplace contract and file names come from
the ingest server. Neither may address anything outside the local
marketplace folders.
"""
import binascii
import importlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import types
from unittest import TestCase

ADDRESS = '0x627306090abaB3A6e1400e9345bC60c78a8BEf57'
CATALYST_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'catalyst')
)


def _load_isolated():
    """
    Load the marketplace modules from the source tree without importing the
    compiled parts of catalyst, for interpreters that cannot build them.
    """
    def package(name, *path):
        mod = types.ModuleType(name)
        mod.__path__ = [os.path.join(CATALYST_DIR, *path)]
        sys.modules[name] = mod

    package('catalyst')
    package('catalyst.utils', 'utils')
    package('catalyst.exchange', 'exchange')
    package('catalyst.exchange.utils', 'exchange', 'utils')
    package('catalyst.marketplace', 'marketplace')
    package('catalyst.marketplace.utils', 'marketplace', 'utils')

    errors = types.ModuleType('catalyst.errors')

    class ZiplineError(Exception):
        msg = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __str__(self):
            return self.msg.format(**self.kwargs)

    errors.ZiplineError = ZiplineError
    sys.modules['catalyst.errors'] = errors

    stats_utils = types.ModuleType('catalyst.exchange.utils.stats_utils')
    stats_utils.set_print_settings = lambda: None
    sys.modules['catalyst.exchange.utils.stats_utils'] = stats_utils

    if 'bcolz' not in sys.modules:
        try:
            import bcolz  # noqa: F401
        except ImportError:
            bcolz = types.ModuleType('bcolz')

            def ctable(*args, **kwargs):
                raise IOError('bcolz is not available')

            bcolz.ctable = ctable
            sys.modules['bcolz'] = bcolz

    return (
        importlib.import_module('catalyst.marketplace.marketplace'),
        importlib.import_module('catalyst.marketplace.utils.path_utils'),
    )


try:
    from catalyst.marketplace import marketplace as mkt_module
    from catalyst.marketplace.utils import path_utils
except ImportError:
    mkt_module, path_utils = _load_isolated()

from catalyst.marketplace.marketplace_errors import (  # noqa: E402
    MarketplaceHTTPRequest,
    MarketplaceInvalidName,
)


class _FakeWeb3(object):
    @staticmethod
    def toHex(value):
        return '0x' + binascii.hexlify(value).decode('ascii')

    @staticmethod
    def toText(value):
        return value.decode('utf-8')


class _Call(object):
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value


class _Functions(object):
    """Contract view that reports every name as registered and paid for."""

    def getDataProviderInfo(self, hex_name):
        return _Call([ADDRESS, 0, 0, 0, True])

    def checkAddressSubscription(self, address, hex_name):
        name = binascii.unhexlify(hex_name[2:])
        return _Call([ADDRESS, name.ljust(32, b'\0'), 0, 0, 0, True])


class _Response(object):
    def __init__(self, content, content_type):
        self.status_code = 200
        self.content = content
        self.encoding = 'utf-8'
        self.headers = {'content-type': content_type}

    def json(self):
        return {'error': 'not a bundle'}


def _tar_gz(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _multipart(filename, payload):
    boundary = 'catalystboundary'
    body = (
        '--{b}\r\n'
        'Content-Disposition: form-data; name="file"; '
        'filename="{f}"\r\n'
        'Content-Type: application/octet-stream\r\n\r\n'
    ).format(b=boundary, f=filename).encode('utf-8')
    body += payload + '\r\n--{b}--\r\n'.format(b=boundary).encode('utf-8')
    return _Response(body, 'multipart/form-data; boundary={}'.format(boundary))


class MarketplaceDatasetPathTestCase(TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, 'home')
        self.root = os.path.join(self.home, '.catalyst')
        self.data = os.path.join(self.root, 'data')
        self.mktplace = os.path.join(self.data, 'marketplace')
        os.makedirs(self.mktplace)

        self.home_canary = os.path.join(self.home, 'canary.txt')
        with open(self.home_canary, 'w') as f:
            f.write('keep me')

        auth_dir = os.path.join(self.data, 'exchanges', 'binance')
        os.makedirs(auth_dir)
        self.auth_json = os.path.join(auth_dir, 'auth.json')
        with open(self.auth_json, 'w') as f:
            f.write('{"key": "k", "secret": "s"}')

        self.outside = os.path.join(self.tmp, 'outside')
        os.makedirs(self.outside)
        self.outside_secret = os.path.join(self.outside, 'secret.txt')
        with open(self.outside_secret, 'w') as f:
            f.write('keep me')

        self._old_root = os.environ.get('ZIPLINE_ROOT')
        os.environ['ZIPLINE_ROOT'] = self.root

        self._old_web3 = getattr(mkt_module, 'Web3', None)
        mkt_module.Web3 = _FakeWeb3
        self._old_requests = mkt_module.requests
        self.posts = []
        self.response = _Response(b'{"error": "x"}', 'application/json')

        def post(*args, **kwargs):
            self.posts.append((args, kwargs))
            return self.response

        mkt_module.requests = _Namespace(post=post)

        self.marketplace = mkt_module.Marketplace.__new__(
            mkt_module.Marketplace
        )
        self.marketplace.mkt_contract = _Namespace(functions=_Functions())
        self.marketplace.addresses = [dict(
            pubAddr=ADDRESS, desc='', wallet='key', key='k', secret='s',
            accepted_terms=True,
        )]
        self.marketplace.choose_pubaddr = lambda: (ADDRESS, 0)

    def tearDown(self):
        mkt_module.requests = self._old_requests
        if self._old_web3 is None:
            del mkt_module.Web3
        else:
            mkt_module.Web3 = self._old_web3
        if self._old_root is None:
            os.environ.pop('ZIPLINE_ROOT', None)
        else:
            os.environ['ZIPLINE_ROOT'] = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ingest(self, ds_name):
        try:
            self.marketplace.ingest(ds_name)
        except Exception as e:
            return e

    def _clean(self, ds_name, data_frequency=None):
        try:
            self.marketplace.clean(ds_name, data_frequency)
        except Exception as e:
            return e

    def assert_untouched(self):
        self.assertTrue(os.path.isfile(self.home_canary))
        self.assertTrue(os.path.isfile(self.auth_json))
        self.assertTrue(os.path.isfile(self.outside_secret))

    def test_ingest_nine_byte_relative_name_deletes_home_canary(self):
        self.assertEqual(len('../../../'.encode()), 9)
        self._ingest('../../../')
        self.assert_untouched()

    def test_ingest_parent_segment_deletes_exchange_credentials(self):
        self._ingest('../exchanges')
        self.assert_untouched()

    def test_ingest_absolute_name_replaces_data_directory(self):
        self._ingest(self.outside)
        self.assert_untouched()

    def test_ingest_nested_parent_segments_escape_prefix_check(self):
        self._ingest('marketcap/../../exchanges')
        self.assert_untouched()

    def test_ingest_rejects_bad_name_before_download(self):
        err = self._ingest('../exchanges')
        self.assertIsInstance(err, MarketplaceInvalidName)
        self.assertEqual(self.posts, [])

    def test_ingest_server_filename_cannot_escape_temp_folder(self):
        target = os.path.join(self.home, 'planted.tar.gz')
        self.response = _multipart(
            '../../../../planted.tar.gz',
            _tar_gz([('price.txt', b'1')]),
        )
        self._ingest('marketcap')
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target[:-len('.tar.gz')]))
        self.assert_untouched()

    def test_ingest_tar_member_cannot_escape_bundle(self):
        self.response = _multipart(
            'marketcap.tar.gz',
            _tar_gz([
                ('price.txt', b'1'),
                ('../../../../../planted.txt', b'x'),
            ]),
        )
        self._ingest('marketcap')
        self.assertFalse(
            os.path.exists(os.path.join(self.home, 'planted.txt'))
        )
        self.assert_untouched()

    def test_plain_dataset_name_leaves_exchange_credentials(self):
        err = self._ingest('marketcap')
        self.assertIsInstance(err, MarketplaceHTTPRequest)
        self.assertEqual(len(self.posts), 1)
        self.assert_untouched()

    def test_plain_dataset_ingest_installs_bundle(self):
        self.response = _multipart(
            'marketcap.tar.gz',
            _tar_gz([('price.txt', b'1')]),
        )
        self.assertIsNone(self._ingest('marketcap'))
        bundle = os.path.join(self.mktplace, 'marketcap', 'price.txt')
        self.assertTrue(os.path.isfile(bundle))
        self.assert_untouched()

    def test_clean_nine_byte_relative_name_deletes_home_canary(self):
        self._clean('../../../')
        self.assert_untouched()

    def test_clean_parent_segment_deletes_exchange_credentials(self):
        self._clean('../exchanges')
        self.assert_untouched()

    def test_clean_absolute_name_replaces_data_directory(self):
        self._clean(self.outside)
        self.assert_untouched()

    def test_clean_nested_parent_segments_escape_prefix_check(self):
        self._clean('marketcap/../../exchanges')
        self.assert_untouched()

    def test_clean_data_frequency_cannot_escape_dataset(self):
        os.makedirs(os.path.join(self.mktplace, 'marketcap'))
        self._clean('marketcap', '../../exchanges')
        self.assert_untouched()

    def test_clean_plain_dataset_name(self):
        folder = os.path.join(self.mktplace, 'marketcap')
        os.makedirs(folder)
        self.assertIsNone(self._clean('marketcap'))
        self.assertFalse(os.path.exists(folder))
        self.assert_untouched()

    def test_process_temp_bundle_does_not_replace_outside_directory(self):
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(empty)
        inode = os.stat(empty).st_ino
        temp = path_utils.get_temp_bundles_folder()
        tar_path = os.path.join(temp, 'evil.tar.gz')
        with open(tar_path, 'wb') as f:
            f.write(_tar_gz([('price.txt', b'1')]))
        try:
            self.marketplace.process_temp_bundle('../../../../empty', tar_path)
        except Exception:
            pass
        self.assertTrue(os.path.isdir(empty))
        self.assertEqual(os.stat(empty).st_ino, inode)
        self.assertEqual(os.listdir(empty), [])

    def test_get_dataset_does_not_create_outside_directory(self):
        created = os.path.join(self.root, 'created_outside')
        try:
            self.marketplace.get_dataset('../../created_outside')
        except Exception:
            pass
        self.assertFalse(os.path.exists(created))

    def test_reserved_marketplace_entries_are_not_dataset_names(self):
        for name in ('temp_bundles', 'addresses.json', '.', '..', '',
                     '.hidden', 'a\\b', 'a\0b'):
            with self.assertRaises(MarketplaceInvalidName):
                path_utils.get_data_source_folder(name)

    def test_symlinked_dataset_folder_is_refused(self):
        if not hasattr(os, 'symlink'):
            return
        os.symlink(self.outside, os.path.join(self.mktplace, 'linked'))
        with self.assertRaises(MarketplaceInvalidName):
            path_utils.get_data_source_folder('linked')
        self._clean('linked')
        self.assert_untouched()


class _Namespace(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
