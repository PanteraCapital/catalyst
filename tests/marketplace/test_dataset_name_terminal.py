import unicodedata
from unittest import TestCase, skipIf

try:
    from unittest import mock
except ImportError:
    import mock

try:
    from web3 import Web3
    from web3.providers.base import BaseProvider
except ImportError:
    Web3 = None
    BaseProvider = object

import catalyst.marketplace.marketplace as marketplace_module
from catalyst.marketplace.marketplace import Marketplace

BUYER = '0x' + '33' * 20
SELLER = '0x' + '44' * 20
MKT_CONTRACT = '0x' + '11' * 20
ENG_CONTRACT = '0x' + '22' * 20

ERC20_APPROVE_ABI = [{
    'constant': False,
    'inputs': [{'name': '_spender', 'type': 'address'},
               {'name': '_value', 'type': 'uint256'}],
    'name': 'approve',
    'outputs': [{'name': '', 'type': 'bool'}],
    'payable': False,
    'stateMutability': 'nonpayable',
    'type': 'function',
}]

APPROVE_SELECTOR = '095ea7b3'

# Moves the cursor to column 0 and paints a fake, cheaper question over
# the real one. Only lowercase characters so that it survives str.lower().
CR_PAYLOAD = 'q\rpay 1 eng? [y] '

HOSTILE_NAMES = [
    CR_PAYLOAD,
    'a\nfake line',
    'b\x1b[2Kc',
    'd\x08\x08e',
    'f\x00g',
    'h\x0ci',
    'j\u202ek',
]


def bytes32(name):
    return name.encode('utf-8').ljust(32, b'\0')


def control_chars(text, allowed=''):
    return [ch for ch in text
            if ch not in allowed and
            unicodedata.category(ch) in ('Cc', 'Cf', 'Zl', 'Zp')]


def render_line(text):
    """What a terminal shows for a single line written by input()/print()."""
    screen = []
    col = 0
    for ch in text:
        if ch == '\r':
            col = 0
        elif ch == '\b':
            col = max(0, col - 1)
        else:
            if col < len(screen):
                screen[col] = ch
            else:
                screen.append(ch)
            col += 1
    return ''.join(screen)


class FakeNode(BaseProvider):
    def __init__(self, balance):
        self.balance = balance

    def isConnected(self):
        return True

    def make_request(self, method, params):
        results = {
            'net_version': '1',
            'eth_gasPrice': hex(10 ** 9),
            'eth_estimateGas': hex(60000),
            'eth_getTransactionCount': '0x0',
            'eth_call': '0x' + '{:064x}'.format(self.balance),
        }
        if method in results:
            return {'jsonrpc': '2.0', 'id': 1, 'result': results[method]}
        return {'jsonrpc': '2.0', 'id': 1,
                'error': {'code': -32000, 'message': 'refused by test'}}


class FakeCall(object):
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value


class FakeMarketplaceFunctions(object):
    def __init__(self, names, grains, registered=True, subscribed=False):
        self.names = names
        self.grains = grains
        self.registered = registered
        self.subscribed = subscribed

    def getProviderNamesSize(self):
        return FakeCall(len(self.names))

    def getNameAt(self, i):
        return FakeCall(bytes32(self.names[i]))

    def getDataProviderInfo(self, name):
        return FakeCall([SELLER, self.grains, 0, 0, self.registered])

    def checkAddressSubscription(self, address, name):
        return FakeCall([address, bytes32('other'), 0,
                         1500000000, 1600000000, self.subscribed])

    def getWithdrawAmount(self, name):
        return FakeCall(0)


@skipIf(Web3 is None, 'The Data Marketplace requires web3 (Python 3).')
class TestDatasetNameTerminal(TestCase):

    def make_marketplace(self, names, grains=5000000 * 10 ** 8,
                         balance=10 ** 30, **kwargs):
        marketplace_module.Web3 = Web3
        mkt = Marketplace.__new__(Marketplace)
        mkt.addresses = [{
            'pubAddr': BUYER, 'desc': '', 'wallet': 'key',
            'accepted_terms': True, 'key': 'k', 'secret': 's',
        }]
        mkt.default_account = BUYER
        mkt.web3 = Web3(FakeNode(balance))
        mkt.mkt_contract_address = Web3.toChecksumAddress(MKT_CONTRACT)
        mkt.mkt_contract = mock.Mock()
        mkt.mkt_contract.functions = FakeMarketplaceFunctions(
            names, grains, **kwargs)
        mkt.eng_contract_address = Web3.toChecksumAddress(ENG_CONTRACT)
        mkt.eng_contract = mkt.web3.eth.contract(
            mkt.eng_contract_address, abi=ERC20_APPROVE_ABI)
        return mkt

    def run_session(self, func, answers):
        """Run func with scripted input(); return (prompts, printed text).

        answers is a list of replies or a callable mapping a prompt to a
        reply. The session ends like a closed stdin once replies run out.
        """
        prompts = []
        printed = []
        if not callable(answers):
            answers = list(answers)

        def fake_input(prompt=''):
            prompts.append(prompt)
            if callable(answers):
                return answers(prompt)
            if not answers:
                raise EOFError()
            return answers.pop(0)

        def fake_print(*args, **kwargs):
            printed.append(kwargs.get('sep', ' ').join(str(a) for a in args)
                           + kwargs.get('end', '\n'))

        with mock.patch.object(marketplace_module, 'input', fake_input,
                               create=True), \
                mock.patch.object(marketplace_module, 'print', fake_print,
                                  create=True), \
                mock.patch.object(marketplace_module.webbrowser,
                                  'open_new'):
            try:
                func()
            except EOFError:
                pass
        return prompts, ''.join(printed)

    def test_subscribe_confirmation_cannot_hide_chain_price(self):
        mkt = self.make_marketplace([CR_PAYLOAD])
        prompts, out = self.run_session(mkt.subscribe, ['0', 'n'])

        confirm = [p for p in prompts if 'agree to pay' in p]
        self.assertEqual(len(confirm), 1, prompts)
        self.assertEqual(control_chars(confirm[0]), [], repr(confirm[0]))
        self.assertIn('5000000 ENG', render_line(confirm[0]))
        self.assertFalse(render_line(confirm[0]).startswith('pay 1 eng?'))
        self.assertEqual(control_chars(out, allowed='\n\t'), [], repr(out))

    def test_subscribe_shows_exact_fractional_price(self):
        mkt = self.make_marketplace(['marketcap'], grains=199999999)
        prompts, out = self.run_session(mkt.subscribe, ['0', 'n'])

        confirm = [p for p in prompts if 'agree to pay' in p][0]
        self.assertIn('1.99999999 ENG', confirm)
        self.assertIn('is 1.99999999 ENG', out)

    def test_approve_instructions_repeat_decimal_eng_beside_calldata(self):
        grains = 5000000 * 10 ** 8
        mkt = self.make_marketplace([CR_PAYLOAD], grains=grains)
        prompts, out = self.run_session(mkt.subscribe, ['0', 'y', '0xdead'])

        lines = out.split('\n')
        data_idx = [i for i, x in enumerate(lines) if x.startswith('Data:')]
        self.assertEqual(len(data_idx), 1, out)
        calldata = lines[data_idx[0]].split()[-1]
        self.assertTrue(calldata.startswith('0x' + APPROVE_SELECTOR))
        self.assertEqual(int(calldata[-64:], 16), grains)

        neighbours = lines[data_idx[0] - 1:data_idx[0] + 2]
        self.assertTrue(
            any("5000000 ENG" in line and "Data:" not in line
                for line in neighbours),
            neighbours)
        value_line = [x for x in lines if x.startswith('Value')][0]
        self.assertTrue(value_line.endswith('0'), value_line)

    def test_catalog_does_not_emit_control_characters(self):
        for func in ('list', 'subscribe', 'ingest',
                     'get_withdraw_amount', 'withdraw'):
            mkt = self.make_marketplace(HOSTILE_NAMES, registered=False)
            method = getattr(mkt, func)
            prompts, out = self.run_session(method, ['0'])
            for text in prompts + [out]:
                self.assertEqual(control_chars(text, allowed='\n\t'), [],
                                 '{}: {!r}'.format(func, text))
            self.assertNotIn('\nfake line', out, func)

    def test_status_lines_do_not_emit_control_characters(self):
        for name in HOSTILE_NAMES:
            for func, kwargs in (
                    ('subscribe', dict(registered=False)),
                    ('subscribe', dict(subscribed=True)),
                    ('ingest', dict(registered=False)),
                    ('ingest', dict()),
                    ('get_withdraw_amount', dict(registered=False)),
                    ('withdraw', dict(registered=False))):
                mkt = self.make_marketplace([name], **kwargs)
                method = getattr(mkt, func)
                prompts, out = self.run_session(
                    lambda: method(name), [])
                self.assertEqual(control_chars(out, allowed='\n\t'), [],
                                 '{} {}: {!r}'.format(func, kwargs, out))

    def test_publish_errors_do_not_emit_control_characters(self):
        for name in HOSTILE_NAMES:
            mkt = self.make_marketplace([name], registered=False)
            with self.assertRaises(Exception) as ctx:
                mkt.publish(name, '/nonexistent', False)
            self.assertEqual(control_chars(str(ctx.exception)), [],
                             repr(str(ctx.exception)))

            mkt = self.make_marketplace([name])
            with self.assertRaises(Exception) as ctx:
                mkt.publish(name, '/nonexistent', False)
            self.assertEqual(control_chars(str(ctx.exception)), [],
                             repr(str(ctx.exception)))

    def register_answers(self, names):
        names = list(names)

        def answer(prompt):
            if 'name of the dataset' in prompt:
                if not names:
                    raise EOFError()
                return names.pop(0)
            if 'price' in prompt:
                return '5'
            if 'frequency' in prompt:
                return 'daily'
            return 'y'
        return answer

    def test_register_does_not_echo_or_register_control_characters(self):
        # Name already taken: the name is echoed back in the error.
        mkt = self.make_marketplace(['x'])
        prompts, out = self.run_session(
            mkt.register, self.register_answers(HOSTILE_NAMES))
        self.assertEqual(control_chars(out, allowed='\n\t'), [], repr(out))

        # Name free: it must not end up in a register transaction.
        for name in HOSTILE_NAMES:
            mkt = self.make_marketplace(['x'], registered=False)
            register = mock.Mock(side_effect=EOFError)
            mkt.mkt_contract.functions.register = register
            prompts, out = self.run_session(
                mkt.register, self.register_answers([name]))
            self.assertFalse(register.called, repr(name))
            self.assertEqual(control_chars(out, allowed='\n\t'), [],
                             repr(out))
