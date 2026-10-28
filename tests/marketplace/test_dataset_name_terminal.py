# Tests for on-chain dataset names that rewrite the subscribe confirmation.
#
# These tests fail while dataset names from the contract can put carriage
# returns, newlines, escapes, or other non-printable bytes into a prompt or
# status line, and while the ENG approve signing text does not repeat the
# decimal price next to the calldata. They are expected to pass once those
# outputs are limited to a visible identifier and the signing instructions
# show the same decimal ENG amount that the approve calldata spends.
#
# Run:
#   python -m unittest tests.marketplace.test_dataset_name_terminal
#   nosetests tests/marketplace/test_dataset_name_terminal.py
#
# Marketplace.clean is not covered: it prints directory names from disk.
# Marketplace.get_dataset is not covered: it does not print the dataset name.
# The second subscribe transaction (the marketplace subscribe call) is not
# covered: it is built only after the approve transaction is mined, and its
# calldata does not carry the ENG allowance.

from __future__ import print_function

import io
import os
import shutil
import sys
import time
import types
import unittest
import uuid
from contextlib import redirect_stdout

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


# Non-printables that must never reach a terminal from a dataset name.
# Newline is included for prompts; status-line checks look for the raw
# payload characters other than the newlines print() itself adds.
_CONTROL_ORDINALS = set(range(32)) | {127}


def _has_control(text):
    return any(ord(ch) in _CONTROL_ORDINALS for ch in text)


def _prepare_import_path():
    """Import the real marketplace module.

    A full Catalyst install imports it directly. This checkout often has no
    compiled extensions, so the heavy package initializer is skipped and only
    marketplace.py plus the modules it needs for this path are loaded.
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if root not in sys.path:
        sys.path.insert(0, root)

    import importlib.util
    if importlib.util.find_spec('bcolz') is not None:
        return

    import collections
    import collections.abc
    for name in ('Sequence', 'Mapping', 'Iterable', 'MutableMapping',
                 'MutableSet'):
        if not hasattr(collections, name):
            setattr(collections, name, getattr(collections.abc, name))

    pkg = types.ModuleType('catalyst')
    pkg.__path__ = [os.path.join(root, 'catalyst')]
    pkg.__package__ = 'catalyst'
    sys.modules['catalyst'] = pkg

    bcolz = types.ModuleType('bcolz')
    sys.modules['bcolz'] = bcolz

    import pandas as pd

    def set_print_settings():
        # Same display options as catalyst.exchange.utils.stats_utils, using
        # the option names this pandas build still accepts.
        pd.set_option('display.expand_frame_repr', False)
        pd.set_option('display.width', 1000)
        pd.set_option('display.max_colwidth', 1000)
        for key in ('display.precision', 'precision'):
            try:
                pd.set_option(key, 8)
            except (KeyError, ValueError, pd.errors.OptionError):
                continue

    for name, path in (
        ('catalyst.exchange', os.path.join(root, 'catalyst', 'exchange')),
        ('catalyst.exchange.utils',
         os.path.join(root, 'catalyst', 'exchange', 'utils')),
    ):
        mod = types.ModuleType(name)
        mod.__path__ = [path]
        mod.__package__ = name
        sys.modules[name] = mod
    stats = types.ModuleType('catalyst.exchange.utils.stats_utils')
    stats.set_print_settings = set_print_settings
    sys.modules['catalyst.exchange.utils.stats_utils'] = stats


_prepare_import_path()

from catalyst.marketplace.marketplace import Marketplace  # noqa: E402
from catalyst.marketplace.utils.eth_utils import from_grains  # noqa: E402
import catalyst.marketplace.marketplace as marketplace_mod  # noqa: E402

# marketplace_errors installs a process-wide excepthook on import.
sys.excepthook = sys.__excepthook__


def _load_codecs():
    """Web3 text/hex helpers from the web3 4.4.1 the project pins.

    web3 4.4.1 assigns Web3.toText/toHex/toInt to web3.utils.encoding, whose
    byte and hexstr conversions match eth_utils.to_text/to_hex/to_int. Use the
    installed Web3 when it imports, and eth_utils (web3 4.4.1 requires
    eth-utils>=1.0.1,<2) otherwise.
    """
    try:
        from web3 import Web3
        return Web3
    except Exception:
        from eth_utils import to_checksum_address, to_hex, to_int, to_text

        class Web3(object):
            toText = staticmethod(to_text)
            toHex = staticmethod(to_hex)
            toInt = staticmethod(to_int)
            toChecksumAddress = staticmethod(to_checksum_address)

        return Web3


Web3 = _load_codecs()
marketplace_mod.Web3 = Web3


def _load_abi():
    try:
        from eth_abi import decode_abi, encode_abi
        return encode_abi, decode_abi
    except ImportError:
        import inspect
        if not hasattr(inspect, 'getargspec'):
            inspect.getargspec = inspect.getfullargspec
        for name in list(sys.modules):
            if name == 'parsimonious' or name.startswith('parsimonious.') \
                    or name == 'eth_abi' or name.startswith('eth_abi.'):
                del sys.modules[name]
        from eth_abi import decode_abi, encode_abi
        return encode_abi, decode_abi


encode_abi, decode_abi = _load_abi()

from eth_utils import function_signature_to_4byte_selector  # noqa: E402

APPROVE_SELECTOR = function_signature_to_4byte_selector(
    'approve(address,uint256)')

CHAIN_PRICE = 5000000
CHAIN_GRAINS = CHAIN_PRICE * 10 ** 8
BUYER = '0x' + ('11' * 20)
MARKET = '0x' + ('22' * 20)
ENG = '0x' + ('33' * 20)


def _bytes32(text):
    raw = text.encode('utf-8')
    if len(raw) > 32:
        raise AssertionError('dataset names are bytes32, got %s' % len(raw))
    return raw.ljust(32, b'\x00')


def _decode_name_arg(value):
    if isinstance(value, bytes):
        raw = value
    else:
        text = value[2:] if isinstance(value, str) and value.startswith('0x') \
            else value
        try:
            raw = bytes.fromhex(text)
        except (TypeError, ValueError):
            raw = text.encode('utf-8')
    return raw.rstrip(b'\x00').decode('utf-8')


class _Call(object):
    def __init__(self, fn):
        self._fn = fn

    def call(self):
        return self._fn()


class _Approve(object):
    def __init__(self, sink, spender, grains):
        self._sink = sink
        self._spender = spender
        self._grains = grains

    def buildTransaction(self, transaction=None):
        # web3 4.4.1 ContractFunction.buildTransaction -> prepare_transaction
        # then fill_transaction_defaults. TRANSACTION_DEFAULTS sets value to 0
        # when the caller omits it. subscribe() passes only from and nonce.
        tx = dict(transaction or {})
        tx.setdefault('value', 0)
        tx.setdefault('to', ENG)
        tx.setdefault('gas', 90000)
        spender = self._spender[2:] if self._spender.startswith('0x') \
            else self._spender
        encoded = encode_abi(
            ['address', 'uint256'],
            [bytes.fromhex(spender), int(self._grains)],
        )
        tx['data'] = '0x' + APPROVE_SELECTOR.hex() + encoded.hex()
        self._sink.append({
            'spender': self._spender,
            'grains': int(self._grains),
            'tx': tx,
        })
        return tx


class _EngFunctions(object):
    def __init__(self, sink):
        self._sink = sink

    def approve(self, spender, grains):
        return _Approve(self._sink, spender, grains)


class _MktFunctions(object):
    def __init__(self, chain):
        self._chain = chain

    def getProviderNamesSize(self):
        return _Call(lambda: 1)

    def getNameAt(self, index):
        return _Call(lambda index=index: self._chain['name_bytes'])

    def getDataProviderInfo(self, key):
        def _info(key=key):
            self._chain['lookups'].append(_decode_name_arg(key))
            if self._chain['is_provider']:
                return (
                    BUYER,
                    self._chain['grains'],
                    0,
                    0,
                    True,
                    True,
                    False,
                )
            return (BUYER, 0, 0, 0, False, False, False)
        return _Call(_info)

    def checkAddressSubscription(self, address, key):
        def _sub(address=address, key=key):
            active = self._chain['subscribed']
            return (
                address,
                self._chain['name_bytes'],
                self._chain['grains'],
                0,
                0,
                active,
                False,
                False,
                False,
            )
        return _Call(_sub)

    def register(self, key, grains, owner):
        def _build(key=key, grains=grains, owner=owner):
            self._chain['registrations'].append(_decode_name_arg(key))
            return {'from': owner, 'to': MARKET, 'value': 0, 'gas': 1,
                    'nonce': 0, 'data': '0x'}
        call = _Call(lambda: None)
        call.buildTransaction = _build
        return call


class _Eth(object):
    def __init__(self):
        self.sent = []

    def call(self, tx):
        return (10 ** 30).to_bytes(32, 'big')

    def getTransactionCount(self, address, block_identifier=None):
        return 7

    def sendRawTransaction(self, raw):
        self.sent.append(raw)
        raise RuntimeError('stop-after-sign')


class _Chain(object):
    def __init__(self, name_text, grains=CHAIN_GRAINS, is_provider=True,
                 subscribed=False):
        self.name_text = name_text
        self.state = {
            'name_bytes': _bytes32(name_text),
            'grains': grains,
            'is_provider': is_provider,
            'subscribed': subscribed,
            'lookups': [],
            'registrations': [],
        }
        self.approvals = []
        self.eth = _Eth()
        self.mkt_functions = _MktFunctions(self.state)
        self.eng_functions = _EngFunctions(self.approvals)

    def marketplace(self):
        market = Marketplace.__new__(Marketplace)
        market.addresses = [{
            'pubAddr': BUYER,
            'wallet': 'metamask',
            'desc': 'buyer',
            'accepted_terms': True,
        }]
        market.default_account = BUYER
        market.mkt_contract_address = MARKET
        market.eng_contract_address = ENG
        market.mkt_contract = types.SimpleNamespace(functions=self.mkt_functions)
        market.eng_contract = types.SimpleNamespace(functions=self.eng_functions)
        market.web3 = types.SimpleNamespace(eth=self.eth)
        return market


class _Answers(object):
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=''):
        self.prompts.append(prompt)
        if not self.answers:
            return 'n'
        return self.answers.pop(0)


def _run(method_name, name_text, answers, **chain_kwargs):
    chain = _Chain(name_text, **chain_kwargs)
    market = chain.marketplace()
    answers = _Answers(answers)
    stdout = io.StringIO()
    with mock.patch('builtins.input', answers), \
            mock.patch.object(marketplace_mod.webbrowser, 'open_new',
                              lambda url: None), \
            redirect_stdout(stdout):
        try:
            getattr(market, method_name)()
        except RuntimeError as exc:
            if 'stop-after-sign' not in str(exc):
                raise
        except Exception:
            # publish raises MarketplaceDatasetNotFound; let callers inspect
            # stdout and re-raise only unexpected errors below.
            raise
    return types.SimpleNamespace(
        chain=chain,
        prompts=answers.prompts,
        stdout=stdout.getvalue(),
        approvals=chain.approvals,
    )


def _confirmation_prompt(prompts):
    for prompt in prompts:
        if 'Signed Transaction' in prompt:
            continue
        if 'Choose the dataset' in prompt or prompt.startswith('Choose your'):
            continue
        if 'agree to pay' in prompt or str(CHAIN_PRICE) in prompt:
            return prompt
    return None


def _signing_block(stdout):
    start = stdout.find('Visit https://legacy.mycrypto.com')
    if start < 0:
        return ''
    end = stdout.find('Copy and Paste', start)
    if end < 0:
        end = len(stdout)
    return stdout[start:end]


def _price_next_to_calldata(block, price):
    lines = block.splitlines()
    token = str(price)
    for index, line in enumerate(lines):
        if line.strip().startswith('Data:'):
            window = '\n'.join(lines[index:index + 2])
            return token in window and 'ENG' in window
    return False


def _decoded_approve_grains(tx):
    raw = bytes.fromhex(tx['data'][2:])
    _spender, amount = decode_abi(['address', 'uint256'], raw[4:])
    return amount


def _tmux_base():
    binary = shutil.which('tmux')
    if not binary and os.path.exists('/exec-daemon/tmux'):
        binary = '/exec-daemon/tmux'
    if not binary:
        raise RuntimeError('tmux is required to render the confirmation line')
    conf = '/exec-daemon/tmux.portal.conf'
    if os.path.exists(conf):
        return [binary, '-f', conf]
    return [binary]


def render_prompt(prompt, width):
    """Display prompt with the real input() on a terminal of the given width.

    subscribe() passes this string to input(), which writes it to the
    terminal. GNU readline (the backend of Python input() on this build)
    leaves carriage returns for the terminal to apply.
    """
    import subprocess
    import tempfile
    session = 'dsname-%s' % uuid.uuid4().hex[:8]
    base = _tmux_base()
    fd, path = tempfile.mkstemp(prefix='ds-prompt-', suffix='.py')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(
                'prompt = %r\n'
                'try:\n'
                '    input(prompt)\n'
                'except EOFError:\n'
                '    pass\n' % (prompt,)
            )
        subprocess.check_call(
            base + ['new-session', '-d', '-s', session, '-x', str(width),
                    '-y', '30'],
        )
        subprocess.check_call(
            base + ['send-keys', '-t', session + ':0.0',
                    'python3 %s' % path, 'C-m'],
        )
        time.sleep(0.8)
        screen = subprocess.check_output(
            base + ['capture-pane', '-t', session + ':0.0', '-p'],
            universal_newlines=True,
        )
        return screen
    finally:
        subprocess.call(base + ['kill-session', '-t', session])
        try:
            os.remove(path)
        except OSError:
            pass


def _width_where_cr_can_cover_price(prompt, price):
    """Terminal width that keeps the price and the first CR on one line."""
    price_at = prompt.find(str(price))
    cr_at = prompt.find('\r')
    if price_at < 0:
        return 120
    end = price_at + len(str(price))
    if cr_at < 0:
        return max(120, end + 5)
    return max(end, cr_at) + 8


class DatasetNameTerminalTests(unittest.TestCase):
    def test_subscribe_confirmation_cannot_hide_chain_price(self):
        # Bytes that survive str.lower, fit in bytes32, and move the cursor
        # to column 0 before a smaller question.
        name = 'q\rpay 1 eng? [y] '
        result = _run('subscribe', name, ['0', 'y', '00'])
        prompt = _confirmation_prompt(result.prompts)
        self.assertIsNotNone(
            result.approvals,
            'subscribe built no approve transaction and showed no '
            'confirmation; refusing the name is safe',
        )
        if not result.approvals:
            self.assertFalse(_has_control(prompt or ''))
            self.assertNotIn('\r', result.stdout)
            return

        self.assertIsNotNone(prompt)
        width = _width_where_cr_can_cover_price(prompt, CHAIN_PRICE)
        screen = render_prompt(prompt, width)
        self.assertIn(
            str(CHAIN_PRICE),
            screen,
            'at width %s the confirmation line no longer shows %s ENG.\n%s'
            % (width, CHAIN_PRICE, screen),
        )
        self.assertFalse(
            _has_control(prompt),
            'confirmation prompt still contains non-printable bytes: %r'
            % (prompt,),
        )
        self.assertNotIn('\r', result.stdout)
        self.assertNotIn('\x1b', result.stdout)

    def test_approve_instructions_repeat_decimal_eng_beside_calldata(self):
        name = 'q\rpay 1 eng? [y] '
        result = _run('subscribe', name, ['0', 'y', '00'])
        if not result.approvals:
            self.assertNotIn('\r', result.stdout)
            return

        approval = result.approvals[0]
        self.assertEqual(
            _decoded_approve_grains(approval['tx']),
            approval['grains'],
        )
        self.assertEqual(approval['grains'], CHAIN_GRAINS)
        self.assertEqual(from_grains(approval['grains']), CHAIN_PRICE)
        self.assertEqual(approval['tx']['value'], 0)
        block = _signing_block(result.stdout)
        self.assertTrue(
            _price_next_to_calldata(block, CHAIN_PRICE),
            'approve signing text does not show %s ENG next to Data:\n%s'
            % (CHAIN_PRICE, block),
        )

    def test_other_controls_cannot_reach_subscribe_prompts(self):
        payloads = [
            'q\npay 1 eng?',
            'q\x08\x08\x08\x08pay 1',
            'q\x1b[2jpay 1',
            'ab\x00cd',
            'q\x0cpay 1',
        ]
        for name in payloads:
            result = _run('subscribe', name, ['0', 'y', '00'])
            for prompt in result.prompts:
                self.assertFalse(
                    _has_control(prompt),
                    'prompt kept controls from %r: %r' % (name, prompt),
                )
            for ordinal in (0, 8, 12, 13, 27, 127):
                self.assertNotIn(chr(ordinal), result.stdout)

    def test_catalog_listing_emits_no_control_characters(self):
        # pandas 0.19.2 pprint_thing escapes tab, CR, and LF in the frame.
        # Escape, backspace, and NUL are written through as themselves.
        name = 'p\x1b[2J\x08\x00x'
        chain = _Chain(name)
        market = chain.marketplace()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            market.list()
        text = stdout.getvalue()
        for ordinal in (0, 8, 27, 127):
            self.assertNotIn(
                chr(ordinal),
                text,
                'list() printed ordinal %s from the dataset name:\n%r'
                % (ordinal, text),
            )

    def test_subscribe_status_lines_emit_no_control_characters(self):
        name = 'q\rhidden'
        missing = _run(
            'subscribe', name, ['0'], is_provider=False, subscribed=False)
        self.assertNotIn('\r', missing.stdout, missing.stdout)
        self.assertFalse(missing.approvals)

        active = _run(
            'subscribe', name, ['0'], is_provider=True, subscribed=True)
        self.assertNotIn('\r', active.stdout, active.stdout)
        self.assertFalse(active.approvals)

    def test_ingest_status_line_emits_no_control_characters(self):
        # ingest() does not lowercase the catalog name (that call is commented
        # out) and prints it in the not-registered line.
        name = 'q\rhidden'
        result = _run('ingest', name, ['0'], is_provider=False)
        self.assertNotIn('\r', result.stdout, result.stdout)
        self.assertNotIn('\x1b', result.stdout)

    def test_withdraw_paths_emit_no_control_characters(self):
        name = 'q\rhidden'
        amount = _run(
            'get_withdraw_amount', name, ['0'], is_provider=False)
        self.assertNotIn('\r', amount.stdout, amount.stdout)

        withdrawn = _run('withdraw', name, ['0'], is_provider=False)
        self.assertNotIn('\r', withdrawn.stdout, withdrawn.stdout)
        self.assertFalse(withdrawn.approvals)

    def test_publish_error_emits_no_control_characters(self):
        name = 'q\rhidden'
        chain = _Chain(name, is_provider=False)
        market = chain.marketplace()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            try:
                market.publish(name, '/tmp', False)
            except Exception as exc:
                message = str(exc)
            else:
                message = ''
        self.assertFalse(_has_control(message), message)
        self.assertNotIn('\r', stdout.getvalue())

    def test_register_echo_does_not_send_or_print_controls(self):
        name = 'q\rpay 1 eng? [y] '
        chain = _Chain(name, is_provider=True)
        market = chain.marketplace()
        answers = _Answers([])
        calls = {'n': 0}

        def _input(prompt=''):
            answers.prompts.append(prompt)
            calls['n'] += 1
            if calls['n'] == 1:
                return name
            raise StopIteration

        stdout = io.StringIO()
        with mock.patch('builtins.input', _input), \
                mock.patch.object(marketplace_mod.webbrowser, 'open_new',
                                  lambda url: None), \
                redirect_stdout(stdout):
            try:
                market.register()
            except StopIteration:
                pass
        self.assertNotIn('\r', stdout.getvalue(), stdout.getvalue())
        for looked_up in chain.state['lookups']:
            self.assertFalse(
                _has_control(looked_up),
                'register sent a control character to the contract: %r'
                % (looked_up,),
            )
        for registered in chain.state['registrations']:
            self.assertFalse(_has_control(registered), registered)


if __name__ == '__main__':
    unittest.main()
