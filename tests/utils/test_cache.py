import os
from shutil import rmtree
from tempfile import mkdtemp
from unittest import TestCase, skipIf

from pandas import DataFrame, Timestamp, Timedelta
from pandas.util.testing import assert_frame_equal

from catalyst.utils.cache import (
    CachedObject,
    Expired,
    ExpiringCache,
    dataframe_cache,
)


class CachedObjectTestCase(TestCase):

    def test_cached_object(self):
        expiry = Timestamp('2014')
        before = expiry - Timedelta('1 minute')
        after = expiry + Timedelta('1 minute')

        obj = CachedObject(1, expiry)

        self.assertEqual(obj.unwrap(before), 1)
        self.assertEqual(obj.unwrap(expiry), 1)  # Unwrap on expiry is allowed.
        with self.assertRaises(Expired) as e:
            obj.unwrap(after)
        self.assertEqual(e.exception.args, (expiry,))


class ExpiringCacheTestCase(TestCase):

    def test_expiring_cache(self):
        expiry_1 = Timestamp('2014')
        before_1 = expiry_1 - Timedelta('1 minute')
        after_1 = expiry_1 + Timedelta('1 minute')

        expiry_2 = Timestamp('2015')
        after_2 = expiry_1 + Timedelta('1 minute')

        expiry_3 = Timestamp('2016')

        cache = ExpiringCache()

        cache.set('foo', 1, expiry_1)
        cache.set('bar', 2, expiry_2)

        self.assertEqual(cache.get('foo', before_1), 1)
        # Unwrap on expiry is allowed.
        self.assertEqual(cache.get('foo', expiry_1), 1)

        with self.assertRaises(KeyError) as e:
            self.assertEqual(cache.get('foo', after_1))
        self.assertEqual(e.exception.args, ('foo',))

        # Should raise same KeyError after deletion.
        with self.assertRaises(KeyError) as e:
            self.assertEqual(cache.get('foo', before_1))
        self.assertEqual(e.exception.args, ('foo',))

        # Second value should still exist.
        self.assertEqual(cache.get('bar', after_2), 2)

        # Should raise similar KeyError on non-existent key.
        with self.assertRaises(KeyError) as e:
            self.assertEqual(cache.get('baz', expiry_3))
        self.assertEqual(e.exception.args, ('baz',))


class DataFrameCacheTestCase(TestCase):

    def setUp(self):
        self.root = mkdtemp()
        self.cache_dir = os.path.join(self.root, 'cache')
        self.frame = DataFrame({'a': [1, 2, 3]})

    def tearDown(self):
        rmtree(self.root)

    def _outside_entries(self):
        return sorted(
            name for name in os.listdir(self.root) if name != 'cache'
        )

    def test_round_trip(self):
        for serialization in ('msgpack', 'pickle'):
            cache = dataframe_cache(self.cache_dir,
                                    serialization=serialization)
            cache['BTC_ETH.daily.frame'] = self.frame
            assert_frame_equal(cache['BTC_ETH.daily.frame'], self.frame)
            self.assertEqual(list(cache), ['BTC_ETH.daily.frame'])
            self.assertEqual(len(cache), 1)

            cache['BTC_ETH.daily.frame'] = self.frame * 2
            assert_frame_equal(cache['BTC_ETH.daily.frame'], self.frame * 2)
            self.assertEqual(list(cache), ['BTC_ETH.daily.frame'])

            del cache['BTC_ETH.daily.frame']
            with self.assertRaises(KeyError):
                cache['BTC_ETH.daily.frame']
            self.assertEqual(len(cache), 0)

    def test_invalid_keys_are_rejected(self):
        cache = dataframe_cache(self.cache_dir)
        victim = os.path.join(self.root, 'victim.frame')
        with open(victim, 'wb') as f:
            f.write(b'original')

        bad_keys = [
            '../victim.frame',
            '../escaped.frame',
            '../../escaped.frame',
            'nested/../../escaped.frame',
            'nested/key.frame',
            os.path.join(self.root, 'absolute.frame'),
            victim,
            '..',
            '.',
            '',
            '...',
            ' .. ',
            'back\\slash.frame',
            'nul\0.frame',
            'C:drive.frame',
            None,
            1,
        ]
        for key in bad_keys:
            with self.assertRaises(ValueError):
                cache[key] = self.frame
            with self.assertRaises(ValueError):
                cache[key]
            with self.assertRaises(ValueError):
                del cache[key]

        with open(victim, 'rb') as f:
            self.assertEqual(f.read(), b'original')
        self.assertEqual(self._outside_entries(), ['victim.frame'])
        self.assertEqual(os.listdir(self.cache_dir), [])

    @skipIf(not hasattr(os, 'symlink'), 'symlinks not supported')
    def test_symlinked_entry_is_not_written_through(self):
        cache = dataframe_cache(self.cache_dir)
        victim = os.path.join(self.root, 'victim.frame')
        with open(victim, 'wb') as f:
            f.write(b'original')
        os.symlink(victim, os.path.join(self.cache_dir, 'BTC_ETH.frame'))

        cache['BTC_ETH.frame'] = self.frame

        with open(victim, 'rb') as f:
            self.assertEqual(f.read(), b'original')
        self.assertFalse(
            os.path.islink(os.path.join(self.cache_dir, 'BTC_ETH.frame')),
        )
        assert_frame_equal(cache['BTC_ETH.frame'], self.frame)

    @skipIf(not hasattr(os, 'symlink'), 'symlinks not supported')
    def test_symlinked_entry_is_not_read_through(self):
        outside = dataframe_cache(os.path.join(self.root, 'outside'))
        outside['secret.frame'] = self.frame
        cache = dataframe_cache(self.cache_dir)
        os.symlink(
            os.path.join(self.root, 'outside', 'secret.frame'),
            os.path.join(self.cache_dir, 'BTC_ETH.frame'),
        )

        with self.assertRaises((KeyError, OSError, IOError)):
            cache['BTC_ETH.frame']

    @skipIf(not hasattr(os, 'symlink'), 'symlinks not supported')
    def test_symlinked_entry_delete_removes_only_the_link(self):
        cache = dataframe_cache(self.cache_dir)
        victim = os.path.join(self.root, 'victim.frame')
        with open(victim, 'wb') as f:
            f.write(b'original')
        os.symlink(victim, os.path.join(self.cache_dir, 'BTC_ETH.frame'))

        del cache['BTC_ETH.frame']

        self.assertTrue(os.path.exists(victim))
        self.assertEqual(os.listdir(self.cache_dir), [])

    def test_failed_serialization_leaves_no_partial_entry(self):
        cache = dataframe_cache(self.cache_dir)
        cache['BTC_ETH.frame'] = self.frame

        def explode(df, buf):
            buf.write(b'partial')
            raise RuntimeError('boom')

        cache.serialize = explode
        with self.assertRaises(RuntimeError):
            cache['BTC_ETH.frame'] = self.frame * 2

        self.assertEqual(os.listdir(self.cache_dir), ['BTC_ETH.frame'])
        cache.serialize = DataFrame.to_msgpack
        assert_frame_equal(cache['BTC_ETH.frame'], self.frame)
