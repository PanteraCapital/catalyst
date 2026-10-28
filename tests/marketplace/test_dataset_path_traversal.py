"""Dataset names must not be used as filesystem paths.

These tests exercise Marketplace.ingest, Marketplace.clean,
Marketplace.process_temp_bundle, and Marketplace.get_dataset. The traversal tests fail while a marketplace
dataset name can escape ~/.catalyst/data/marketplace and create or delete
files outside that directory. A plain dataset name is covered so a fix
still allows that name to affect only its own folder.

clean(..., data_frequency) is not covered. get_bundle_folder is decorated
with @deprecated instead of @deprecated(), so the call raises TypeError
before shutil.rmtree. publish does not delete a local dataset path.

The ingest cases speak HTTP to a local server. The on-chain calls are
answered with the same bytes32 round-trip web3 and eth_abi produce for
the name the client sends, because register() stores that bytes32 with
no character check. Nothing here contacts a remote marketplace.
"""

from __future__ import print_function

import asyncio
import collections
import collections.abc
import inspect
import os
import shutil
import tarfile
import threading
import types
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

# web3 4.4.1 and requests-toolbelt 0.8.0 import names removed in Python 3.12.
# The shims only restore those names so the pinned libraries can load.
for _name in dir(collections.abc):
    if not _name.startswith('_') and not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))
if not hasattr(inspect, 'getargspec'):
    def _getargspec(func):
        spec = inspect.getfullargspec(func)
        return spec.args, spec.varargs, spec.varkw, spec.defaults
    inspect.getargspec = _getargspec
if not hasattr(asyncio, 'coroutine'):
    asyncio.coroutine = lambda fn: fn

import urllib3.contrib
if 'urllib3.contrib.appengine' not in __import__('sys').modules:
    _appengine = types.ModuleType('urllib3.contrib.appengine')
    __import__('sys').modules['urllib3.contrib.appengine'] = _appengine
    urllib3.contrib.appengine = _appengine

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

if 'bcolz' not in sys.modules:
    try:
        import bcolz  # noqa: F401
    except ImportError:
        sys.modules['bcolz'] = types.ModuleType('bcolz')

if 'catalyst' not in sys.modules:
    _catalyst = types.ModuleType('catalyst')
    _catalyst.__path__ = [os.path.join(sys.path[0], 'catalyst')]
    _catalyst.__package__ = 'catalyst'
    sys.modules['catalyst'] = _catalyst

try:
    import catalyst.marketplace.marketplace as marketplace_module
except ImportError:
    _stats = types.ModuleType('catalyst.exchange.utils.stats_utils')

    def _set_print_settings():
        raise AssertionError('dataset listing was not part of this call')

    _stats.set_print_settings = _set_print_settings
    sys.modules['catalyst.exchange.utils.stats_utils'] = _stats
    import catalyst.marketplace.marketplace as marketplace_module

from eth_abi import decode_abi, encode_abi
from web3 import Web3

from catalyst.marketplace.utils.path_utils import get_marketplace_folder

marketplace_module.Web3 = Web3

SECRET = '{"key": "exchange-api-key", "secret": "exchange-api-secret"}\n'
HOME_CANARY = 'home-directory-canary\n'
ADDRESS = '0x1111111111111111111111111111111111111111'


class _IngestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        if length:
            self.rfile.read(length)
        self.server.paths.append(self.path)
        body = b'not-a-bundle'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class _Call(object):
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _ContractFunctions(object):
    """Return values shaped like Marketplace.sol getDataProviderInfo
    and checkAddressSubscription. The returned bytes32 is the ABI
    encoding of the same string the client passed to Web3.toHex.
    """

    def __init__(self, dataset_name, address):
        self.dataset_name = dataset_name
        self.address = address
        self.encoded_names = []

    def _word(self):
        encoded = encode_abi(['bytes32'], [self.dataset_name.encode('utf-8')])
        return decode_abi(['bytes32'], encoded)[0]

    def getDataProviderInfo(self, encoded_name):
        self.encoded_names.append(encoded_name)
        # owner, price, volume, subscriptionsNum, isProvider, isActive, isPunished
        return _Call((self.address, 1, 0, 1, True, True, False))

    def checkAddressSubscription(self, address, encoded_name):
        self.encoded_names.append(encoded_name)
        # subscriber, dataSourceName, price, startTime, endTime,
        # isUnExpired, isPaid, isPunishedProvider, isOrder
        return _Call((
            address, self._word(), 1, 0, 0, True, False, False, True,
        ))


class _Contract(object):
    def __init__(self, dataset_name, address):
        self.functions = _ContractFunctions(dataset_name, address)


def _bytes32_fits(name):
    # BytesEncoder rejects values longer than the bytes32 width.
    return len(name.encode('utf-8')) <= 32


class DatasetPathTraversalTests(unittest.TestCase):
    def setUp(self):
        self._old_root = os.environ.get('ZIPLINE_ROOT')
        self._old_auth = marketplace_module.AUTH_SERVER
        self.sandbox = tempfile_root()
        self.home = os.path.join(self.sandbox, 'home')
        self.zipline_root = os.path.join(self.home, '.catalyst')
        os.makedirs(self.home)
        os.environ['ZIPLINE_ROOT'] = self.zipline_root
        self.httpd = None
        self._server_thread = None

    def tearDown(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self._server_thread.join(timeout=5)
            self.httpd.server_close()
        marketplace_module.AUTH_SERVER = self._old_auth
        if self._old_root is None:
            os.environ.pop('ZIPLINE_ROOT', None)
        else:
            os.environ['ZIPLINE_ROOT'] = self._old_root
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def _start_ingest_server(self):
        self.httpd = HTTPServer(('127.0.0.1', 0), _IngestHandler)
        self.httpd.paths = []
        self._server_thread = threading.Thread(target=self.httpd.serve_forever)
        self._server_thread.daemon = True
        self._server_thread.start()
        port = self.httpd.server_address[1]
        marketplace_module.AUTH_SERVER = 'http://127.0.0.1:{0}'.format(port)

    def _client(self, dataset_name):
        self.assertTrue(
            _bytes32_fits(dataset_name),
            'name is not a bytes32 and could not be registered',
        )
        client = marketplace_module.Marketplace.__new__(
            marketplace_module.Marketplace)
        client.addresses = [{
            'pubAddr': ADDRESS,
            'wallet': 'metamask',
            'key': 'hmac-key',
            'secret': 'hmac-secret',
            'accepted_terms': True,
        }]
        client.mkt_contract = _Contract(dataset_name, ADDRESS)
        return client

    def _prepare_marketplace_tree(self):
        """Match Marketplace.__init__, which creates the marketplace folder
        while loading addresses.json. Parent segments do not resolve until
        that directory exists.
        """
        get_marketplace_folder()
        auth_dir = os.path.join(
            self.zipline_root, 'data', 'exchanges', 'binance')
        os.makedirs(auth_dir)
        auth_path = os.path.join(auth_dir, 'auth.json')
        with open(auth_path, 'w') as handle:
            handle.write(SECRET)
        canary = os.path.join(self.home, 'canary.txt')
        with open(canary, 'w') as handle:
            handle.write(HOME_CANARY)
        return auth_path, canary

    def _joined(self, dataset_name):
        return os.path.join(
            self.zipline_root, 'data', 'marketplace', dataset_name)

    def _guard(self, dataset_name, allowed_root):
        normalized = os.path.normpath(self._joined(dataset_name))
        allowed = os.path.realpath(allowed_root)
        if os.path.commonpath([normalized, allowed]) != allowed:
            raise AssertionError(
                'refusing to touch a path outside the sandbox: {0}'.format(
                    normalized))
        return normalized

    def _assert_file(self, path, content):
        self.assertTrue(
            os.path.isfile(path),
            'file was removed: {0}'.format(path),
        )
        with open(path) as handle:
            self.assertEqual(handle.read(), content)

    def _ingest(self, dataset_name, allowed_root):
        self._guard(dataset_name, allowed_root)
        self._start_ingest_server()
        client = self._client(dataset_name)
        # Subscription comparison uses the real Web3.toText. It has to
        # reproduce the dataset name or ingest returns before the delete.
        word = client.mkt_contract.functions._word()
        self.assertEqual(Web3.toText(word).rstrip('\0'), dataset_name)
        try:
            client.ingest(dataset_name)
        except Exception:
            pass
        self.assertTrue(
            client.mkt_contract.functions.encoded_names,
            'ingest never encoded the dataset name',
        )
        self.assertEqual(
            client.mkt_contract.functions.encoded_names[0],
            Web3.toHex(dataset_name.encode('utf-8')),
        )
        return client

    def test_ingest_parent_segment_deletes_exchange_credentials(self):
        auth_path, _canary = self._prepare_marketplace_tree()
        # marketplace/.. is the Catalyst data directory.
        self._ingest('..', self.sandbox)
        self._assert_file(auth_path, SECRET)

    def test_ingest_nine_byte_relative_name_deletes_home_canary(self):
        _auth_path, canary = self._prepare_marketplace_tree()
        # Nine bytes, the short name from the finding.
        # marketplace/../../../ is the simulated home directory.
        self.assertEqual(len(b'../../../'), 9)
        normalized = self._guard('../../../', self.sandbox)
        self.assertEqual(normalized, os.path.normpath(self.home))
        self._ingest('../../../', self.sandbox)
        self._assert_file(canary, HOME_CANARY)

    def test_ingest_absolute_name_replaces_data_directory(self):
        victim = os.path.join('/tmp', 'cmpw' + uuid.uuid4().hex[:8])
        self.assertLessEqual(len(victim.encode('utf-8')), 32)
        os.makedirs(victim)
        secret = os.path.join(victim, 'secret.txt')
        with open(secret, 'w') as handle:
            handle.write(SECRET)
        self.addCleanup(lambda: shutil.rmtree(victim, ignore_errors=True))
        self._ingest(victim, victim)
        self._assert_file(secret, SECRET)
        self.assertFalse(
            os.path.isdir(victim) and not os.path.isfile(secret),
            'absolute dataset name deleted the directory',
        )

    def test_ingest_nested_parent_segments_escape_prefix_check(self):
        auth_path, _canary = self._prepare_marketplace_tree()
        name = 'foo/../../exchanges'
        self.assertTrue(_bytes32_fits(name))
        joined = self._joined(name)
        # A check that only asks whether the joined string starts with the
        # marketplace directory still accepts this name. The delete follows
        # the parent segments.
        marketplace = os.path.join(self.zipline_root, 'data', 'marketplace')
        self.assertTrue(joined.startswith(marketplace))
        self._ingest(name, self.sandbox)
        self._assert_file(auth_path, SECRET)
        self.assertFalse(
            os.path.isdir(os.path.join(marketplace, 'foo')),
            'ingest created a directory for the dataset name before refusing',
        )

    def test_plain_dataset_name_leaves_exchange_credentials(self):
        auth_path, _canary = self._prepare_marketplace_tree()
        self._ingest('ohlcv', self.sandbox)
        self._assert_file(auth_path, SECRET)
        escaped = os.path.normpath(self._joined('ohlcv'))
        marketplace = os.path.join(self.zipline_root, 'data', 'marketplace')
        self.assertTrue(
            escaped == marketplace or escaped.startswith(marketplace + os.sep)
        )

    def test_clean_parent_segment_deletes_exchange_credentials(self):
        auth_path, _canary = self._prepare_marketplace_tree()
        self._guard('..', self.sandbox)
        client = self._client('..')
        try:
            client.clean('..')
        except Exception:
            pass
        self._assert_file(auth_path, SECRET)

    def test_clean_nine_byte_relative_name_deletes_home_canary(self):
        _auth_path, canary = self._prepare_marketplace_tree()
        normalized = self._guard('../../../', self.sandbox)
        self.assertEqual(normalized, os.path.normpath(self.home))
        client = self._client('../../../')
        try:
            client.clean('../../../')
        except Exception:
            pass
        self._assert_file(canary, HOME_CANARY)

    def test_clean_absolute_name_deletes_that_directory(self):
        victim = os.path.join('/tmp', 'cmpw' + uuid.uuid4().hex[:8])
        self.assertLessEqual(len(victim.encode('utf-8')), 32)
        os.makedirs(victim)
        secret = os.path.join(victim, 'secret.txt')
        with open(secret, 'w') as handle:
            handle.write(SECRET)
        self.addCleanup(lambda: shutil.rmtree(victim, ignore_errors=True))
        self._guard(victim, victim)
        client = self._client(victim)
        try:
            client.clean(victim)
        except Exception:
            pass
        self._assert_file(secret, SECRET)

    def test_process_temp_bundle_does_not_replace_outside_directory(self):
        get_marketplace_folder()
        outside = os.path.join(self.zipline_root, 'data', 'outside_empty')
        os.makedirs(outside)
        original_inode = os.stat(outside).st_ino
        bundle = os.path.join(self.sandbox, 'incoming', 'bundle.tar.gz')
        os.makedirs(os.path.dirname(bundle))
        with tarfile.open(bundle, 'w:gz') as archive:
            payload = os.path.join(self.sandbox, 'incoming', 'marker.txt')
            with open(payload, 'w') as handle:
                handle.write('bundle')
            archive.add(payload, arcname='marker.txt')
        name = '../outside_empty'
        self._guard(name, self.sandbox)
        client = self._client(name)
        try:
            client.process_temp_bundle(name, bundle)
        except Exception:
            pass
        self.assertTrue(os.path.isdir(outside))
        self.assertEqual(os.stat(outside).st_ino, original_inode)
        self.assertEqual(os.listdir(outside), [])

    def test_get_dataset_does_not_create_outside_directory(self):
        get_marketplace_folder()
        outside = os.path.join(
            self.zipline_root, 'data', 'created_by_get_dataset')
        name = '../created_by_get_dataset'
        self._guard(name, self.sandbox)
        client = self._client(name)
        try:
            client.get_dataset(name)
        except Exception:
            pass
        self.assertFalse(
            os.path.exists(outside),
            'get_dataset created a directory outside the marketplace folder',
        )


def tempfile_root():
    import tempfile
    return tempfile.mkdtemp(prefix='catalyst-dataset-path-')


if __name__ == '__main__':
    unittest.main()
