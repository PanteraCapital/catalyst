import binascii


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
    Convert from grains to cryptocurrency

    Parameters
    ----------
    input: amount

    Returns
    -------
    int

    """
    return amount // 10 ** 8


def format_grains(amount):
    """
    Format an amount of grains as an exact decimal ENG amount

    Parameters
    ----------
    input: amount

    Returns
    -------
    str

    """
    eng, fraction = divmod(amount, 10 ** 8)
    if not fraction:
        return str(eng)
    return '{}.{}'.format(eng, '{:08d}'.format(fraction).rstrip('0'))


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
    return amount * 10 ** 8
