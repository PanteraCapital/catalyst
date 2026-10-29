import json
import os
import shutil
import tempfile
from unittest import TestCase

import pandas as pd
from mock import Mock, patch

import catalyst.curate.poloniex as curator
from catalyst.curate.poloniex import PoloniexCurator
from catalyst.data.bundles.poloniex import PoloniexBundle
from catalyst.utils.cache import dataframe_cache
from catalyst.utils.calendars import get_calendar


START = pd.Timestamp('2018-01-01', tz='utc')
END = pd.Timestamp('2018-01-03', tz='utc')


def _ticker_json(*symbols):
    return json.dumps({
        sym: {'id': i, 'isFrozen': 0, 'last': '0.1'}
        for i, sym in enumerate(symbols)
    })


def _chart_json():
    return json.dumps([
        {
            'date': int(day.value // 10 ** 9),
            'open': 1.0,
            'high': 2.0,
            'low': 0.5,
            'close': 1.5,
            'volume': 10.0,
        }
        for day in pd.date_range(START, END, freq='D')
    ])


class _ConsumingWriter(object):
    def __init__(self):
        self.written = []

    def write(self, data, *args, **kwargs):
        if not isinstance(data, pd.DataFrame):
            data = list(data)
        self.written.append(data)


class _IgnoringWriter(object):
    def write(self, *args, **kwargs):
        pass


class PoloniexCompileCacheTestCase(TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cache_dir = os.path.join(self.root, 'cache', 'poloniex')

    def tearDown(self):
        shutil.rmtree(self.root)

    def _files_outside_cache(self):
        return sorted(
            os.path.join(dirpath, name)
            for dirpath, _, names in os.walk(self.root)
            if not (dirpath + os.sep).startswith(self.cache_dir + os.sep)
            for name in names
        )

    def _ingest(self, *symbols):
        bundle = PoloniexBundle()
        daily_writer = _ConsumingWriter()
        asset_db_writer = _ConsumingWriter()

        with patch.object(PoloniexBundle, '_format_metadata_url',
                          lambda self, api_key, page: _ticker_json(*symbols)),\
                patch.object(PoloniexBundle, '_format_data_url',
                             lambda self, *args: _chart_json()), \
                patch.object(PoloniexBundle, 'wait_time',
                             pd.Timedelta(0)), \
                dataframe_cache(self.cache_dir,
                                clean_on_failure=False) as cache:
            bundle.ingest(
                environ={},
                asset_db_writer=asset_db_writer,
                minute_bar_writer=_IgnoringWriter(),
                daily_bar_writer=daily_writer,
                adjustment_writer=_IgnoringWriter(),
                calendar=get_calendar('OPEN'),
                start_session=START,
                end_session=END,
                cache=cache,
                show_progress=False,
                is_compile=True,
                output_dir=self.root,
            )
            keys = set(cache)

        return keys, daily_writer, asset_db_writer

    def test_legitimate_symbols_are_cached(self):
        keys, daily_writer, asset_db_writer = self._ingest('BTC_ETH')

        self.assertIn('BTC_ETH.daily.frame', keys)
        self.assertEqual(len(daily_writer.written), 1)
        self.assertEqual(len(daily_writer.written[0]), 1)
        self.assertEqual(
            list(asset_db_writer.written[0].symbol), ['BTC_ETH'],
        )

    def test_parent_segments_do_not_create_a_frame_outside_the_cache(self):
        escaped = os.path.join(self.root, 'cache', 'victim.daily.frame')

        self._ingest('../victim', 'BTC_ETH')

        self.assertFalse(os.path.exists(escaped))
        self.assertEqual(self._files_outside_cache(), [])

    def test_parent_segments_do_not_overwrite_a_frame_outside_the_cache(self):
        other_cache = os.path.join(self.root, 'cache', 'other')
        stale = pd.DataFrame(
            {'close': [1.0]},
            index=pd.DatetimeIndex([pd.Timestamp('2010-01-01')]),
        )
        dataframe_cache(other_cache)['USD.daily.frame'] = stale
        victim = os.path.join(other_cache, 'USD.daily.frame')
        with open(victim, 'rb') as f:
            original = f.read()

        self._ingest('../other/USD', 'BTC_ETH')

        with open(victim, 'rb') as f:
            self.assertEqual(f.read(), original)

    def test_parent_segments_do_not_overwrite_a_file_outside_the_cache(self):
        victim = os.path.join(self.root, 'victim.daily.frame')
        open(victim, 'wb').close()

        self._ingest('../../victim', 'BTC_ETH')

        self.assertEqual(os.path.getsize(victim), 0)

    def test_absolute_symbol_does_not_write_outside_the_cache(self):
        target = os.path.join(self.root, 'absolute')

        self._ingest(target, 'BTC_ETH')

        self.assertFalse(os.path.exists(target + '.daily.frame'))
        self.assertEqual(self._files_outside_cache(), [])


class PoloniexCuratorPathTestCase(TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.csv_dir = os.path.join(self.root, 'csv') + os.sep
        os.makedirs(os.path.join(self.csv_dir, 'crypto_trades-x'))
        self.patcher = patch.object(curator, 'CSV_OUT_FOLDER', self.csv_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.root)

    def test_csv_paths_reject_unsafe_currency_pairs(self):
        for pair in ('x/../../escaped', '../escaped', 'a/b', 'a\\b', 'C:x',
                     os.path.join(self.root, 'abs')):
            with self.assertRaises(ValueError):
                curator._csv_path('trades', pair)
            with self.assertRaises(ValueError):
                PoloniexCurator().onemin_to_dataframe(pair, START, END)

        self.assertEqual(
            curator._csv_path('1min', 'BTC_ETH'),
            self.csv_dir + 'crypto_1min-BTC_ETH.csv',
        )

    def test_retrieve_trade_history_does_not_write_outside_the_folder(self):
        rejected = False
        with patch.object(curator.requests, 'get',
                          side_effect=RuntimeError('offline')) as get:
            try:
                PoloniexCurator().retrieve_trade_history('x/../../escaped')
            except ValueError:
                rejected = True

        self.assertEqual(sorted(os.listdir(self.root)), ['csv'])
        self.assertTrue(rejected)
        get.assert_not_called()

    def test_unsafe_tickers_are_skipped(self):
        response = Mock()
        response.json.return_value = json.loads(
            _ticker_json('BTC_ETH', 'x/../../escaped', '..', 'BTC_XMR'),
        )
        pc = PoloniexCurator()
        with patch.object(curator.requests, 'get', return_value=response):
            pc.get_currency_pairs()

        self.assertEqual(pc.currency_pairs, ['BTC_ETH', 'BTC_XMR'])
