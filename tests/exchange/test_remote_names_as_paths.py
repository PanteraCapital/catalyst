import os
import shutil
import tempfile
from unittest import TestCase

import pandas as pd
from mock import patch

import catalyst.exchange.utils.bundle_utils as bundle_utils
from catalyst.exchange.utils.exchange_utils import save_asset_data


class GetBcolzChunkTestCase(TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.bundles = os.path.join(self.root, 'bundles')
        os.makedirs(os.path.join(self.bundles, 'poloniex-daily-x'))

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_symbol_cannot_leave_the_bundles_folder(self):
        with patch.object(bundle_utils, 'get_exchange_bundles_folder',
                          return_value=self.bundles), \
                patch.object(bundle_utils, 'download_without_progress',
                             side_effect=RuntimeError('offline')) as dl:
            with self.assertRaises(ValueError):
                bundle_utils.get_bcolz_chunk(
                    'poloniex', 'x/../../escaped', 'daily', '2017-10',
                )
        dl.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.root)), ['bundles'])

    def test_existing_chunk_is_reused(self):
        existing = os.path.join(self.bundles, 'poloniex-daily-eth_btc-2017-10')
        os.makedirs(existing)
        with patch.object(bundle_utils, 'get_exchange_bundles_folder',
                          return_value=self.bundles), \
                patch.object(bundle_utils, 'download_without_progress') as dl:
            path = bundle_utils.get_bcolz_chunk(
                'poloniex', 'eth_btc', 'daily', '2017-10',
            )
        self.assertEqual(path, existing)
        dl.assert_not_called()


class SaveAssetDataTestCase(TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.folder = os.path.join(self.root, 'assets')
        os.makedirs(self.folder)

    def tearDown(self):
        shutil.rmtree(self.root)

    def _frame(self, symbol):
        return pd.DataFrame(
            {'close': [1.0]},
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp('2018-01-01'), symbol)],
                names=['date', 'symbol'],
            ),
        )

    def test_symbol_cannot_leave_the_folder(self):
        rejected = False
        try:
            save_asset_data(self.folder, self._frame('../escaped'))
        except ValueError:
            rejected = True

        self.assertEqual(sorted(os.listdir(self.root)), ['assets'])
        self.assertEqual(os.listdir(self.folder), [])
        self.assertTrue(rejected)

    def test_symbol_is_saved(self):
        save_asset_data(self.folder, self._frame('eth_btc'))
        self.assertEqual(os.listdir(self.folder), ['eth_btc.csv'])
