def is_printable_name(name):
    """
    Whether a dataset name contains only characters that a terminal
    displays as themselves (no control, format or separator characters).

    Parameters
    ----------
    name: str

    Returns
    -------
    bool

    """
    return all(ch.isprintable() for ch in name)


def display_name(name):
    """
    Make a dataset name safe to write to a terminal.

    Dataset names come from the marketplace contract and are chosen by
    whoever registered them. Characters that a terminal would interpret
    (carriage return, newline, escape sequences, bidi overrides, ...)
    are replaced by visible backslash escapes, so the name cannot move the
    cursor or overwrite surrounding text. Backslashes and double quotes
    are escaped too so that the result is unambiguous inside quotes.

    Parameters
    ----------
    name: str

    Returns
    -------
    str

    """
    out = []
    for ch in name:
        if ch in ('\\', '"'):
            out.append('\\' + ch)
        elif ch.isprintable():
            out.append(ch)
        else:
            code = ord(ch)
            if code <= 0xff:
                out.append('\\x{:02x}'.format(code))
            elif code <= 0xffff:
                out.append('\\u{:04x}'.format(code))
            else:
                out.append('\\U{:08x}'.format(code))
    return ''.join(out)
