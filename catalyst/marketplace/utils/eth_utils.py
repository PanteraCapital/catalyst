import binascii
from decimal import Decimal

ENG_DECIMALS = 8


# def bytes32(string):
#     """
#     Convert string to bytes32 data type for smart contract

#     Parameters
#     ----------
#     string: str

#     Returns
#     -------
#     list

#     """
#     return binascii.hexlify(string.encode('utf-8'))


# def b32_str(bytes32):
#     """
#     Convert bytes32 to string

#     Parameters
#     ----------
#     input: bytes object

#     Returns
#     -------
#     str

#     """
#     return binascii.unhexlify(
#             bytes32.decode('utf-8').rstrip('\0')).decode('ascii')


def bin_hex(binary):
    """
    Convert bytes32 to string

    Parameters
    ----------
    input: bytes object

    Returns
    -------
    str

    """
    return binascii.hexlify(binary).decode('utf-8')


def from_grains(amount):
    """
    Convert from grains to cryptocurrency, without losing any grains

    Parameters
    ----------
    input: amount

    Returns
    -------
    Decimal

    """
    return Decimal('{}E-{}'.format(int(amount), ENG_DECIMALS))


def format_grains(amount):
    """
    Format a grain amount as an exact cryptocurrency string

    Parameters
    ----------
    input: amount

    Returns
    -------
    str

    """
    amount = int(amount)
    sign = '-' if amount < 0 else ''
    whole, fraction = divmod(abs(amount), 10 ** ENG_DECIMALS)
    if not fraction:
        return '{}{}'.format(sign, whole)
    return '{}{}.{}'.format(
        sign, whole,
        '{:0{}d}'.format(fraction, ENG_DECIMALS).rstrip('0'))


def to_grains(amount):
    """
    Convert from cryptocurrency to grains

    Parameters
    ----------
    input: amount

    Returns
    -------
    int

    """
    return amount * 10 ** ENG_DECIMALS
