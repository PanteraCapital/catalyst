"""Spec tests for cache paths built from remote symbols.

These tests fail while a symbol from the Poloniex ticker payload can be
joined onto the compile-time cache directory and written, read, or deleted
outside that directory. They pass once cache keys are a single safe path
segment and the opened path stays inside the cache directory.

A check that resolves the path and then opens the original path still fails
the symlink case: opening the original path follows the link and writes the
outside file before any later refusal.

Not covered here:
- catalyst/data/bundles/yahoo.py `_cachpath` replaces `os.path.sep` before
  the key is used, so it does not share this join.
- catalyst/curate/poloniex.py concatenates currency pairs into CSV paths
  under CSV_OUT_FOLDER. Minute compile reaches that only after the daily
  cache write, through PoloniexCurator, and these tests stop at daily bars.
- Quandl metadata uses the same BaseBundle._maybe_update_symbol_frame cache
  key as Poloniex. The Poloniex compile tests execute that method.
"""
from __future__ import print_function

import json
import os
import shutil
import tempfile
from unittest import TestCase
from six.moves.urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from pandas.io import common as pd_common

from catalyst.data.bundles.core import ingest as bundle_ingest
from catalyst.data.bundles.core import register_bundle
from catalyst.data.bundles.poloniex import PoloniexBundle
from catalyst.utils.cache import dataframe_cache
from catalyst.utils.memoize import lazyval


_SENTINEL = b'ORIGINAL-SENTINEL'
_OUTSIDE_NAME = 'outside.daily.frame'


class _BytesResponse(object):
    """Body returned by pandas.io.common._urlopen for an http(s) URL.

    pandas 0.19.2 get_filepath_or_buffer reads that body with .read().
    """

    def __init__(self, payload):
        self._payload = payload

    def read(self, *args, **kwargs):
        return self._payload

    def close(self):
        return None


class _DailyPoloniex(PoloniexBundle):
    """Poloniex metadata and daily bars only.

    Minute bars call PoloniexCurator, which writes exchange CSVs. The cache
    key for minute data is built by the same BaseBundle method as daily data.
    """

    @lazyval
    def frequencies(self):
        return set(('daily',))


def _ticker_payload(symbol):
    # Field names match PoloniexBundle.fetch_raw_metadata_frame, which keeps
    # rows whose isFrozen compares equal to 0. The public payload uses a
    # string "0"; pandas 0.19.2 read_json coerces that to an integer.
    return json.dumps({
        symbol: {
            'id': 1,
            'last': '1.0',
            'lowestAsk': '1.0',
            'highestBid': '1.0',
            'percentChange': '0.0',
            'baseVolume': '1.0',
            'quoteVolume': '1.0',
            'isFrozen': '0',
            'high24hr': '1.0',
            'low24hr': '1.0',
        },
    }).encode('utf-8')


def _chart_payload():
    # Columns consumed by PoloniexBundle.fetch_raw_symbol_frame for daily
    # bars. The date sits inside the OPEN calendar used by compile.
    return json.dumps([{
        'date': '2016-06-01',
        'high': 2.0,
        'low': 1.0,
        'open': 1.5,
        'close': 1.7,
        'volume': 10.0,
        'quoteVolume': 15.0,
        'weightedAverage': 1.6,
    }]).encode('utf-8')


def _snapshot(path):
    st = os.stat(path)
    mtime = getattr(st, 'st_mtime_ns', st.st_mtime)
    if os.path.isdir(path):
        return ('dir', st.st_ino, mtime, tuple(sorted(os.listdir(path))))
    with open(path, 'rb') as handle:
        payload = handle.read()
    return ('file', st.st_ino, mtime, st.st_size, payload)


def _frame():
    return pd.DataFrame(
        {
            'open': [1.5],
            'high': [2.0],
            'low': [1.0],
            'close': [1.7],
            'volume': [10.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp('2016-01-01')]),
    )


class PoloniexCompileCacheEscapeTestCase(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='compile-cache-escape-')
        self.vault = os.path.join(self.root, 'vault')
        os.makedirs(self.vault)
        self._real_urlopen = pd_common._urlopen
        self._real_request = requests.sessions.Session.request
        register_bundle(_DailyPoloniex, create_writers=True)

    def tearDown(self):
        pd_common._urlopen = self._real_urlopen
        requests.sessions.Session.request = self._real_request
        shutil.rmtree(self.root, ignore_errors=True)

    def _block_other_network(self):
        def request(session, method, url, *args, **kwargs):
            raise AssertionError(
                'compile test tried to call the network: {0} {1}'.format(
                    method, url,
                )
            )

        requests.sessions.Session.request = request

    def _serve_poloniex(self, symbol):
        ticker = _ticker_payload(symbol)
        chart = _chart_payload()

        def urlopen(url, *args, **kwargs):
            raw = url if isinstance(url, str) else url.get_full_url()
            parsed = urlparse(raw)
            if parsed.scheme not in ('http', 'https'):
                raise AssertionError(raw)
            if parsed.netloc != 'poloniex.com':
                raise AssertionError(raw)
            command = parse_qs(parsed.query)['command'][0]
            if command == 'returnTicker':
                return _BytesResponse(ticker)
            if command == 'returnChartData':
                return _BytesResponse(chart)
            raise AssertionError(raw)

        pd_common._urlopen = urlopen
        self._block_other_network()

    def _compile(self, symbol):
        self._serve_poloniex(symbol)
        bundle_ingest(
            'poloniex',
            environ={'ZIPLINE_ROOT': self.root},
            timestamp=pd.Timestamp('2018-01-02', tz='UTC'),
            show_progress=False,
            is_compile=True,
        )

    def test_parent_segments_do_not_create_a_frame_outside_the_cache(self):
        target = os.path.join(self.vault, _OUTSIDE_NAME)
        before = _snapshot(self.vault)
        # Three ".." components leave data/poloniex/.cache and land in vault.
        self._compile('../../../vault/outside')
        self.assertFalse(os.path.exists(target))
        self.assertEqual(_snapshot(self.vault), before)

    def test_parent_segments_do_not_overwrite_a_frame_outside_the_cache(self):
        target = os.path.join(self.vault, _OUTSIDE_NAME)
        _frame().to_msgpack(target)
        before = _snapshot(target)
        self._compile('../../../vault/outside')
        self.assertEqual(_snapshot(target), before)
        self.assertEqual(_snapshot(self.vault)[3], (_OUTSIDE_NAME,))

    def test_plain_ticker_does_not_write_the_vault(self):
        before = _snapshot(self.vault)
        self._compile('BTC_ETH')
        self.assertEqual(_snapshot(self.vault), before)
        cache_dir = os.path.join(self.root, 'data', 'poloniex', '.cache')
        # A successful compile removes the cache directory on the way out.
        # The safe name must not have escaped into the bundle directory.
        bundle_dir = os.path.join(self.root, 'data', 'poloniex')
        escaped = [
            name for name in os.listdir(bundle_dir)
            if name.endswith('.frame')
        ]
        self.assertEqual(escaped, [])
        if os.path.isdir(cache_dir):
            outside = []
            for dirpath, dirnames, filenames in os.walk(cache_dir):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if os.path.realpath(full) != full:
                        real = os.path.realpath(full)
                        if not real.startswith(os.path.realpath(cache_dir) + os.sep):
                            outside.append(full)
            self.assertEqual(outside, [])


class DataFrameCachePathTestCase(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='df-cache-escape-')
        self.cache_dir = os.path.join(self.root, 'cache')
        self.vault = os.path.join(self.root, 'vault')
        os.makedirs(self.cache_dir)
        os.makedirs(self.vault)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _assign(self, cache, key, value):
        try:
            cache[key] = value
        except Exception:
            return

    def _seed_marker(self):
        target = os.path.join(self.vault, 'marker.frame')
        # Distinct from the frame assigned below, so a rewrite is visible
        # even when the timestamp does not move.
        seeded = _frame()
        seeded['open'] = 42.0
        seeded.to_msgpack(target)
        return target, _snapshot(target)

    def test_parent_key_set_get_and_delete_stay_inside_the_cache(self):
        key = '../vault/marker.frame'
        cases = (
            ('msgpack', 'set'),
            ('msgpack', 'get'),
            ('msgpack', 'delete'),
            ('pickle', 'set'),
            ('pickle', 'delete'),
        )
        for serialization, operation in cases:
            with self.subTest(serialization=serialization, operation=operation):
                target, before = self._seed_marker()
                cache = dataframe_cache(
                    self.cache_dir,
                    clean_on_failure=False,
                    serialization=serialization,
                )
                if operation == 'set':
                    self._assign(cache, key, _frame())
                elif operation == 'get':
                    try:
                        loaded = cache[key]
                    except Exception:
                        loaded = None
                    self.assertFalse(isinstance(loaded, pd.DataFrame))
                else:
                    try:
                        del cache[key]
                    except Exception:
                        pass
                self.assertTrue(os.path.exists(target))
                self.assertEqual(_snapshot(target), before)

    def test_absolute_key_does_not_write_outside_the_cache(self):
        target = os.path.join(self.vault, 'absolute.frame')
        before_dir = _snapshot(self.vault)
        cache = dataframe_cache(self.cache_dir, clean_on_failure=False)
        self._assign(cache, target, _frame())
        self.assertFalse(os.path.exists(target))
        self.assertEqual(_snapshot(self.vault), before_dir)

    def test_symlink_component_is_not_opened_outside_the_cache(self):
        # The key has no ".." text. Resolving cache/alias follows a symlink
        # out of the cache. A fix that checks the joined path and then opens
        # that same path again still truncates the outside file.
        target = os.path.join(self.vault, 'secret.frame')
        with open(target, 'wb') as handle:
            handle.write(_SENTINEL)
        before = _snapshot(target)
        os.symlink(self.vault, os.path.join(self.cache_dir, 'alias'))
        cache = dataframe_cache(self.cache_dir, clean_on_failure=False)
        self._assign(cache, 'alias/secret.frame', _frame())
        self.assertEqual(_snapshot(target), before)
        try:
            del cache['alias/secret.frame']
        except Exception:
            pass
        self.assertEqual(_snapshot(target), before)

    def test_nested_parent_segments_do_not_leave_the_cache(self):
        target = os.path.join(self.vault, 'nested.frame')
        with open(target, 'wb') as handle:
            handle.write(_SENTINEL)
        before = _snapshot(target)
        # The missing intermediate directory would make open() fail before
        # the ".." components are walked. Create it so the write is attempted.
        os.makedirs(os.path.join(self.cache_dir, 'subdir'))
        cache = dataframe_cache(self.cache_dir, clean_on_failure=False)
        self._assign(cache, 'subdir/../../vault/nested.frame', _frame())
        self.assertEqual(_snapshot(target), before)
