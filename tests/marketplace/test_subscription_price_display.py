# These tests are the spec for the subscription price display bug.
# They fail while the client shows floor(grains / 10**8) ENG but still
# builds an ENG approve for the full grain count, and they pass once the
# amount the buyer confirms is the amount the approve transaction authorizes
# and a price change after that confirmation aborts before any signature.
#
# Run (web3==4.4.1, the version pinned in etc/requirements.txt):
#   python -m unittest tests.marketplace.test_subscription_price_display -v

from __future__ import print_function

import builtins
import io
import json
import os
import re
import sys
import types
import unittest
from decimal import Decimal
from contextlib import redirect_stdout

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Importing catalyst/__init__.py loads Cython extensions that this path never
# calls. Register the package first so marketplace.py loads without that.
if 'catalyst' not in sys.modules:
    _catalyst = types.ModuleType('catalyst')
    _catalyst.__path__ = [os.path.join(REPO_ROOT, 'catalyst')]
    _catalyst.__package__ = 'catalyst'
    sys.modules['catalyst'] = _catalyst

# bcolz is imported by marketplace.py and used only while ingesting a bundle.
# subscribe and get_withdraw_amount do not call it.
if 'bcolz' not in sys.modules:
    sys.modules['bcolz'] = types.ModuleType('bcolz')

# stats_utils pulls the exchange package. subscribe calls set_print_settings
# only when the dataset argument is omitted; these tests always pass a name.
_stats_name = 'catalyst.exchange.utils.stats_utils'
if _stats_name not in sys.modules:
    _exchange = types.ModuleType('catalyst.exchange')
    _exchange.__path__ = [os.path.join(REPO_ROOT, 'catalyst', 'exchange')]
    _exchange_utils = types.ModuleType('catalyst.exchange.utils')
    _exchange_utils.__path__ = [
        os.path.join(REPO_ROOT, 'catalyst', 'exchange', 'utils')
    ]
    _stats = types.ModuleType(_stats_name)

    def _set_print_settings():
        raise AssertionError(
            'set_print_settings is not on the priced subscribe path'
        )

    _stats.set_print_settings = _set_print_settings
    sys.modules['catalyst.exchange'] = _exchange
    sys.modules['catalyst.exchange.utils'] = _exchange_utils
    sys.modules[_stats_name] = _stats

from eth_abi import decode_abi, encode_abi  # noqa: E402
from eth_utils import (  # noqa: E402
    decode_hex,
    function_abi_to_4byte_selector,
    to_checksum_address,
)
from web3 import Web3  # noqa: E402
from web3.providers.base import BaseProvider  # noqa: E402

import catalyst.marketplace.marketplace as marketplace_module  # noqa: E402
from catalyst.marketplace.marketplace import Marketplace  # noqa: E402

# One ENG is 10**8 grains. eth_utils.from_grains / to_grains use this scale.
GRAINS_PER_ENG = 10 ** 8

MARKETPLACE_ABI = json.load(open(os.path.join(
    REPO_ROOT, 'catalyst', 'marketplace', 'contract_marketplace_abi.json')))
ENG_ABI = json.load(open(os.path.join(
    REPO_ROOT, 'catalyst', 'marketplace', 'contract_enigma_abi.json')))

BUYER = to_checksum_address('0x' + '11' * 20)
MARKETPLACE = to_checksum_address('0x' + '22' * 20)
ENG_TOKEN = to_checksum_address('0x' + '33' * 20)
PUBLISHER = to_checksum_address('0x' + '44' * 20)

PRICE_RE = re.compile(
    r'The price for a monthly subscription to this dataset is '
    r'([0-9]+(?:\.[0-9]+)?) ENG'
)
CONFIRM_RE = re.compile(
    r'Please confirm that you agree to pay ([0-9]+(?:\.[0-9]+)?) ENG '
    r'for a monthly subscription to the dataset "([^"]+)"'
)
SPEND_RE = re.compile(
    r'to spend ([0-9]+(?:\.[0-9]+)?) ENG on your behalf'
)
BALANCE_LINE_RE = re.compile(
    r'greater than ([0-9]+(?:\.[0-9]+)?) ENG'
)
INSUFFICIENT_RE = re.compile(
    r'balance is ([0-9]+(?:\.[0-9]+)?) ENG,.*?buy: ([0-9]+(?:\.[0-9]+)?) ENG',
    re.S,
)


def _abi_function(abi, name):
    for item in abi:
        if item.get('type') == 'function' and item.get('name') == name:
            return item
    raise KeyError(name)


def _selector(abi, name):
    return function_abi_to_4byte_selector(_abi_function(abi, name))


def _output_types(abi, name):
    return [output['type'] for output in _abi_function(abi, name)['outputs']]


def _input_types(abi, name):
    return [item['type'] for item in _abi_function(abi, name)['inputs']]


SELECTORS = {
    'getDataProviderInfo': _selector(MARKETPLACE_ABI, 'getDataProviderInfo'),
    'checkAddressSubscription': _selector(
        MARKETPLACE_ABI, 'checkAddressSubscription'),
    'getWithdrawAmount': _selector(MARKETPLACE_ABI, 'getWithdrawAmount'),
    'balanceOf': _selector(ENG_ABI, 'balanceOf'),
    'approve': _selector(ENG_ABI, 'approve'),
    'subscribe': _selector(MARKETPLACE_ABI, 'subscribe'),
}


def grains_from_displayed_eng(text):
    """Convert a displayed ENG amount back to grains. Rejects rounding."""
    amount = Decimal(text)
    scaled = amount * Decimal(GRAINS_PER_ENG)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise AssertionError(
            'displayed ENG %s is not an integer number of grains' % text
        )
    return int(integral)


def _hex_quantity(value):
    return hex(value)


def _call_payload(data):
    if isinstance(data, str):
        return decode_hex(data)
    return bytes(data)


class ScriptedChain(BaseProvider):
    """Answers the JSON-RPC calls subscribe makes.

    Price reads of getDataProviderInfo before the buyer confirms return
    base_price. After confirmation, read number ``diverge_on_post_confirm_read``
    (1-based) and every later read return diverged_price. A value of None
    keeps the price stable.
    """

    def __init__(self, price, balance, withdraw_amount=0,
                 diverge_on_post_confirm_read=None, diverged_price=None):
        self.base_price = price
        self.balance = balance
        self.withdraw_amount = withdraw_amount
        self.diverge_on_post_confirm_read = diverge_on_post_confirm_read
        self.diverged_price = diverged_price
        self.confirmed = False
        self.post_confirm_price_reads = 0
        self.price_reads = []
        self.raw_transactions = []
        self.methods = []

    def confirm(self):
        self.confirmed = True

    def current_price(self):
        if not self.confirmed or self.diverge_on_post_confirm_read is None:
            price = self.base_price
        else:
            self.post_confirm_price_reads += 1
            if self.post_confirm_price_reads >= self.diverge_on_post_confirm_read:
                price = self.diverged_price
            else:
                price = self.base_price
        self.price_reads.append(price)
        return price

    def make_request(self, method, params):
        self.methods.append(method)
        if method == 'eth_call':
            result = self._eth_call(params[0])
        elif method == 'eth_getBlockByNumber':
            result = {
                'number': _hex_quantity(16),
                'gasLimit': _hex_quantity(8000000),
                'extraData': '0x',
                'gasUsed': '0x0',
                'timestamp': '0x0',
                'size': '0x0',
                'difficulty': '0x0',
                'totalDifficulty': '0x0',
                'transactions': [],
                'uncles': [],
            }
        elif method == 'eth_estimateGas':
            result = _hex_quantity(21000)
        elif method == 'eth_gasPrice':
            result = _hex_quantity(1)
        elif method == 'eth_getTransactionCount':
            result = _hex_quantity(len(self.raw_transactions))
        elif method == 'eth_sendRawTransaction':
            self.raw_transactions.append(params[0])
            result = '0x' + ('ab' * 32)
        elif method == 'eth_getTransactionReceipt':
            result = {
                'status': '0x1',
                'transactionHash': '0x' + ('ab' * 32),
                'blockNumber': _hex_quantity(16),
                'gasUsed': _hex_quantity(21000),
                'cumulativeGasUsed': _hex_quantity(21000),
            }
        else:
            raise AssertionError('unexpected RPC %s %r' % (method, params))
        return {'jsonrpc': '2.0', 'id': 1, 'result': result}

    def isConnected(self):
        return True

    def _eth_call(self, transaction):
        payload = _call_payload(transaction['data'])
        selector, arguments = payload[:4], payload[4:]
        if selector == SELECTORS['getDataProviderInfo']:
            price = self.current_price()
            encoded = encode_abi(
                _output_types(MARKETPLACE_ABI, 'getDataProviderInfo'),
                [PUBLISHER, price, 0, 0, True, True, False],
            )
        elif selector == SELECTORS['checkAddressSubscription']:
            encoded = encode_abi(
                _output_types(MARKETPLACE_ABI, 'checkAddressSubscription'),
                [
                    BUYER,
                    b'\x00' * 32,
                    0,
                    0,
                    0,
                    False,  # isUnExpired, index 5; True aborts subscribe
                    False,
                    False,
                    False,
                ],
            )
        elif selector == SELECTORS['balanceOf']:
            (who,) = decode_abi(['address'], arguments)
            if who.lower() != BUYER.lower():
                raise AssertionError(
                    'balanceOf queried %s, buyer is %s' % (who, BUYER)
                )
            encoded = encode_abi(['uint256'], [self.balance])
        elif selector == SELECTORS['getWithdrawAmount']:
            encoded = encode_abi(['uint256'], [self.withdraw_amount])
        else:
            raise AssertionError(
                'eth_call selector %s is not a marketplace or ENG call '
                'this flow makes' % selector.hex()
            )
        return '0x' + encoded.hex()


def _make_marketplace(chain):
    # subscribe uses the Web3 global that Marketplace.__init__ imports.
    # Skip __init__: it downloads the live contract and prompts for terms.
    marketplace_module.Web3 = Web3
    web3 = Web3(chain)
    marketplace = Marketplace.__new__(Marketplace)
    marketplace.addresses = [{
        'pubAddr': BUYER,
        'desc': 'buyer',
        'wallet': 'metamask',
        'accepted_terms': True,
    }]
    marketplace.web3 = web3
    marketplace.mkt_contract_address = MARKETPLACE
    marketplace.eng_contract_address = ENG_TOKEN
    marketplace.mkt_contract = web3.eth.contract(
        address=MARKETPLACE, abi=MARKETPLACE_ABI)
    marketplace.eng_contract = web3.eth.contract(
        address=ENG_TOKEN, abi=ENG_ABI)
    return marketplace


def _decode_call(tx, abi, name):
    payload = _call_payload(tx['data'])
    selector = payload[:4]
    if selector != SELECTORS[name]:
        raise AssertionError(
            'transaction to %s is not %s (selector %s)' % (
                tx.get('to'), name, selector.hex())
        )
    return decode_abi(_input_types(abi, name), payload[4:])


class SubscriptionPriceDisplayTests(unittest.TestCase):
    def setUp(self):
        self.browser_opens = []
        self.prompts = []
        self.signed_txs = []
        self._original_open = marketplace_module.webbrowser.open_new
        self._original_input = builtins.input
        self._original_sleep = marketplace_module.time.sleep
        marketplace_module.webbrowser.open_new = self._open_new
        builtins.input = self._input
        marketplace_module.time.sleep = self._sleep

    def tearDown(self):
        marketplace_module.webbrowser.open_new = self._original_open
        builtins.input = self._original_input
        marketplace_module.time.sleep = self._original_sleep

    def _open_new(self, url):
        self.browser_opens.append(url)
        return True

    def _input(self, prompt=''):
        # input() writes its prompt to stdout before reading. The confirm
        # line is that prompt, so keep it in the captured output.
        self.prompts.append(prompt)
        sys.stdout.write(prompt)
        if 'agree to pay' in prompt:
            self.chain.confirm()
            return 'y'
        if 'Signed Transaction' in prompt:
            return '0x' + ('11' * 32)
        raise AssertionError('unexpected prompt: %r' % prompt)

    def _sleep(self, seconds):
        raise AssertionError(
            'subscribe waited %s seconds instead of finishing the transaction'
            % seconds
        )

    def _run_subscribe(self, chain, dataset='MarketCap'):
        self.chain = chain
        self.signed_txs = []
        marketplace = _make_marketplace(chain)
        original_sign = marketplace.sign_transaction

        def sign_and_record(tx):
            # Snapshot the transaction the real signer prints and asks
            # the buyer to authorize. sign_transaction does not copy it.
            self.signed_txs.append(dict(tx))
            return original_sign(tx)

        marketplace.sign_transaction = sign_and_record
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            marketplace.subscribe(dataset)
        return stdout.getvalue()

    def _assert_approve_matches_confirmed_price(self, output, chain, dataset):
        price_line = PRICE_RE.search(output)
        balance_line = BALANCE_LINE_RE.search(output)
        confirm = CONFIRM_RE.search(output)
        spend = SPEND_RE.search(output)
        self.assertIsNotNone(
            price_line, 'subscribe did not print a subscription price: %s'
            % output)
        self.assertIsNotNone(
            balance_line, 'subscribe did not print the balance check: %s'
            % output)
        self.assertIsNotNone(
            confirm, 'subscribe did not ask the buyer to confirm a price: %s'
            % output)
        self.assertIsNotNone(
            spend, 'subscribe did not tell the buyer what the approve spends: %s'
            % output)

        shown = [
            grains_from_displayed_eng(price_line.group(1)),
            grains_from_displayed_eng(balance_line.group(1)),
            grains_from_displayed_eng(confirm.group(1)),
            grains_from_displayed_eng(spend.group(1)),
        ]
        self.assertEqual(
            confirm.group(2), dataset.lower(),
            'confirmation named a different dataset than the one requested',
        )
        self.assertEqual(
            len(set(shown)), 1,
            'price, balance check, confirmation, and approve description '
            'disagree: %s' % shown,
        )
        confirmed_grains = shown[0]
        self.assertEqual(
            confirmed_grains, chain.base_price,
            'buyer was shown %s grains but the on-chain price they were '
            'asked about was %s' % (confirmed_grains, chain.base_price),
        )

        self.assertGreaterEqual(
            len(self.signed_txs), 1,
            'buyer was not asked to sign the approve transaction',
        )
        self.assertEqual(
            len(self.browser_opens), len(self.signed_txs),
            'signing prompt and browser launches disagree',
        )
        self._assert_printed_tx(output, self.signed_txs[0])

        approve_tx = self.signed_txs[0]
        self.assertEqual(approve_tx['to'].lower(), ENG_TOKEN.lower())
        spender, value = _decode_call(approve_tx, ENG_ABI, 'approve')
        self.assertEqual(spender.lower(), MARKETPLACE.lower())
        self.assertEqual(
            value, confirmed_grains,
            'buyer confirmed %s grains but the signed approve authorizes %s'
            % (confirmed_grains, value),
        )
        if chain.diverged_price is not None:
            self.assertNotEqual(
                value, chain.diverged_price,
                'approve used a price read after the check',
            )

        self.assertGreaterEqual(len(self.signed_txs), 2)
        self._assert_printed_tx(output, self.signed_txs[1])
        subscribe_tx = self.signed_txs[1]
        self.assertEqual(subscribe_tx['to'].lower(), MARKETPLACE.lower())
        (raw_name,) = _decode_call(
            subscribe_tx, MARKETPLACE_ABI, 'subscribe')
        self.assertEqual(
            raw_name.rstrip(b'\x00'), dataset.lower().encode('utf-8'),
            'subscribe transaction is for a different dataset than the '
            'buyer confirmed',
        )
        self.assertEqual(len(chain.raw_transactions), 2)

    def _assert_printed_tx(self, output, tx):
        data = tx['data']
        if not isinstance(data, str):
            data = data.hex()
        self.assertIn(data, output)
        self.assertIn(tx['to'], output)

    def test_whole_token_price_approve_matches_confirmed_amount(self):
        price = 5 * GRAINS_PER_ENG
        chain = ScriptedChain(price=price, balance=price + 1)
        output = self._run_subscribe(chain)
        self._assert_approve_matches_confirmed_price(output, chain, 'MarketCap')

    def test_fractional_grain_price_approve_matches_confirmed_amount(self):
        # N ENG plus one grain short of another ENG: the largest amount
        # from_grains can hide under N.
        price = 5 * GRAINS_PER_ENG + GRAINS_PER_ENG - 1
        chain = ScriptedChain(price=price, balance=price + 1)
        output = self._run_subscribe(chain)
        self._assert_approve_matches_confirmed_price(output, chain, 'MarketCap')

    def test_price_just_under_one_eng_is_not_shown_as_zero(self):
        price = GRAINS_PER_ENG - 1
        chain = ScriptedChain(price=price, balance=price + 1)
        output = self._run_subscribe(chain)
        self._assert_approve_matches_confirmed_price(output, chain, 'MarketCap')

    def test_subscribe_refuses_when_price_changes_after_confirmation(self):
        price = 5 * GRAINS_PER_ENG + GRAINS_PER_ENG - 1
        changed = price + GRAINS_PER_ENG
        chain = ScriptedChain(
            price=price,
            balance=changed + 1,
            diverge_on_post_confirm_read=1,
            diverged_price=changed,
        )
        output = self._run_subscribe(chain)
        confirm = CONFIRM_RE.search(output)
        self.assertIsNotNone(confirm, output)
        self.assertEqual(
            grains_from_displayed_eng(confirm.group(1)), price,
            'confirmation did not show the price that was on chain',
        )
        self.assertEqual(
            self.signed_txs, [],
            'buyer was asked to sign after the on-chain price changed',
        )
        self.assertEqual(self.browser_opens, [])
        self.assertEqual(chain.raw_transactions, [])

    def test_subscribe_does_not_approve_a_price_reread_after_the_check(self):
        # The first getDataProviderInfo after confirmation still matches the
        # price the buyer was shown. A later read returns a different price.
        # Approving that later value is a check-then-use miss.
        price = 5 * GRAINS_PER_ENG + GRAINS_PER_ENG - 1
        changed = price + 1
        chain = ScriptedChain(
            price=price,
            balance=price + 1,
            diverge_on_post_confirm_read=2,
            diverged_price=changed,
        )
        output = self._run_subscribe(chain)
        confirm = CONFIRM_RE.search(output)
        self.assertIsNotNone(confirm, output)
        confirmed = grains_from_displayed_eng(confirm.group(1))
        self.assertEqual(confirmed, price)
        if not self.signed_txs:
            self.assertEqual(self.browser_opens, [])
            self.assertEqual(chain.raw_transactions, [])
            return
        self._assert_printed_tx(output, self.signed_txs[0])
        _spender, value = _decode_call(self.signed_txs[0], ENG_ABI, 'approve')
        self.assertEqual(value, confirmed)
        self.assertNotEqual(value, changed)

    def test_insufficient_balance_message_uses_full_grain_amounts(self):
        price = 5 * GRAINS_PER_ENG + GRAINS_PER_ENG - 1
        # Enough for the truncated whole-token price, not for the grain price.
        balance = 5 * GRAINS_PER_ENG
        chain = ScriptedChain(price=price, balance=balance)
        output = self._run_subscribe(chain)
        match = INSUFFICIENT_RE.search(output)
        self.assertIsNotNone(
            match, 'expected the insufficient-balance message, got: %s' % output
        )
        self.assertEqual(grains_from_displayed_eng(match.group(1)), balance)
        self.assertEqual(grains_from_displayed_eng(match.group(2)), price)
        self.assertFalse(
            any('Signed Transaction' in prompt for prompt in self.prompts)
        )
        self.assertEqual(chain.raw_transactions, [])
        self.assertEqual(self.browser_opens, [])

    def test_withdraw_amount_displays_full_grain_amount(self):
        grains = 5 * GRAINS_PER_ENG + GRAINS_PER_ENG - 1
        chain = ScriptedChain(
            price=grains, balance=0, withdraw_amount=grains)
        self.chain = chain
        marketplace = _make_marketplace(chain)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            marketplace.get_withdraw_amount('MarketCap')
        output = stdout.getvalue().strip()
        match = re.match(r'^([0-9]+(?:\.[0-9]+)?) ENG$', output)
        self.assertIsNotNone(
            match, 'withdraw amount was not printed as ENG: %r' % output
        )
        self.assertEqual(grains_from_displayed_eng(match.group(1)), grains)
        self.assertEqual(chain.raw_transactions, [])
        self.assertEqual(self.browser_opens, [])


if __name__ == '__main__':
    unittest.main()
