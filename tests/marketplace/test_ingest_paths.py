import os
import shutil
import tempfile
from unittest import TestCase

from mock import MagicMock, Mock, patch

import catalyst.marketplace.marketplace as marketplace_module
from catalyst.marketplace.marketplace import Marketplace


class MarketplaceIngestPathTestCase(TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.temp_bundles = os.path.join(self.root, 'temp_bundles')
        self.data_source = os.path.join(self.root, 'marketplace', 'ds')
        os.makedirs(self.temp_bundles)

    def tearDown(self):
        shutil.rmtree(self.root)

    def _ingest(self, filename):
        from web3 import Web3

        address = '0x' + '1' * 40
        market = Marketplace.__new__(Marketplace)
        market.addresses = [
            {'pubAddr': address, 'key': 'key', 'secret': 'secret'},
        ]
        market.mkt_contract = MagicMock()
        fns = market.mkt_contract.functions
        fns.getDataProviderInfo.return_value.call.return_value = \
            [None, None, None, None, True]
        fns.checkAddressSubscription.return_value.call.return_value = \
            [address, Web3.toHex(b'ds'), None, None, None, True]

        part = Mock()
        part.headers = {
            b'Content-Disposition':
                'form-data; name="file"; filename="{}"'.format(
                    filename).encode('utf-8'),
        }
        part.content = b'remote contents'
        decoder = Mock()
        decoder.parts = [part]
        response = Mock()
        response.status_code = 200

        processed = []
        with patch.object(marketplace_module, 'Web3', Web3, create=True), \
                patch.object(marketplace_module.requests, 'post',
                             return_value=response), \
                patch.object(marketplace_module.MultipartDecoder,
                             'from_response', return_value=decoder), \
                patch.object(marketplace_module, 'get_temp_bundles_folder',
                             return_value=self.temp_bundles), \
                patch.object(marketplace_module, 'get_data_source_folder',
                             return_value=self.data_source), \
                patch.object(Marketplace, 'process_temp_bundle',
                             lambda self, ds, path: processed.append(path)):
            market.ingest('ds')
        return processed

    def test_part_filename_cannot_leave_the_temp_folder(self):
        victim = os.path.join(self.root, 'victim')
        with open(victim, 'wb') as f:
            f.write(b'original')

        rejected = False
        try:
            self._ingest('../victim')
        except ValueError:
            rejected = True

        with open(victim, 'rb') as f:
            self.assertEqual(f.read(), b'original')
        self.assertEqual(os.listdir(self.temp_bundles), [])
        self.assertTrue(rejected)

    def test_part_is_written_to_the_temp_folder(self):
        processed = self._ingest('ds-2018.tar.gz')

        expected = os.path.join(self.temp_bundles, 'ds-2018.tar.gz')
        self.assertEqual(processed, [expected])
        with open(expected, 'rb') as f:
            self.assertEqual(f.read(), b'remote contents')
