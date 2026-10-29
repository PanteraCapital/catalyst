import errno
import json
import os
import re
import shutil
import unicodedata

import numpy as np

# bcolz unpickles every element of a carray whose dtype has objects, and it
# takes that dtype from the ``meta/storage`` file found in the carray's root.
BCOLZ_STORAGE_FILE = 'storage'

_SEPARATORS = re.compile(r'[\\/]')


class UnsafeArchiveError(ValueError):
    pass


def _path_parts(name):
    """Split an archive member name into components that stay under the
    extraction root on every supported platform, or raise.
    """
    if not name or '\x00' in name:
        raise UnsafeArchiveError('invalid member name: {!r}'.format(name))
    if name[0] in '/\\':
        raise UnsafeArchiveError('absolute member name: {!r}'.format(name))

    parts = []
    for part in _SEPARATORS.split(name):
        if part in ('', '.'):
            continue
        # Windows drops trailing dots and spaces, so '.. ' resolves to '..',
        # and a colon introduces a drive or an alternate data stream.
        if part.rstrip(' .') == '' or ':' in part:
            raise UnsafeArchiveError(
                'member name escapes the destination: {!r}'.format(name)
            )
        parts.append(part)
    return parts


def check_filename(name):
    """Return ``name`` if it is a single plain path component, else raise."""
    parts = _path_parts(name)
    if len(parts) != 1 or parts[0] != name:
        raise UnsafeArchiveError('unsafe file name: {!r}'.format(name))
    return name


def _is_bcolz_storage(parts):
    # Case-insensitive filesystems open 'meta/storage' for names that only
    # match after case folding, so compare the folded name.
    name = parts[-1].rstrip(' .')
    if isinstance(name, bytes):
        name = name.decode('utf-8', 'replace')
    name = unicodedata.normalize('NFKC', name)
    name = name.casefold() if hasattr(name, 'casefold') else name.lower()
    return name == BCOLZ_STORAGE_FILE


def _check_bcolz_storage(name, content):
    try:
        meta = json.loads(content.decode('ascii'))
        dtype = np.dtype(meta['dtype'])
    except Exception:
        raise UnsafeArchiveError(
            'unreadable bcolz storage metadata: {!r}'.format(name)
        )
    if dtype.hasobject:
        raise UnsafeArchiveError(
            'bcolz column with object dtype: {!r}'.format(name)
        )


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST or not os.path.isdir(path):
            raise


def safe_extract(tar, path):
    """Extract an untrusted tar archive under ``path``.

    Every member is checked before anything is written. Only regular files
    and directories whose names stay under ``path`` are accepted, and bcolz
    columns that would be unpickled when read are refused. Ownership,
    permissions and timestamps from the archive are not applied.

    Parameters
    ----------
    tar : tarfile.TarFile
        An archive opened for reading.
    path : str
        The destination directory.

    Raises
    ------
    UnsafeArchiveError
        If any member is not safe to extract. Nothing is written in that
        case.
    """
    plan = []
    for member in tar.getmembers():
        parts = _path_parts(member.name)
        if member.isdir():
            plan.append((member, parts, None))
        elif member.isfile():
            if not parts:
                raise UnsafeArchiveError(
                    'file member without a name: {!r}'.format(member.name)
                )
            content = None
            if _is_bcolz_storage(parts):
                content = tar.extractfile(member).read()
                _check_bcolz_storage(member.name, content)
            plan.append((member, parts, content))
        else:
            raise UnsafeArchiveError(
                'unsupported member type for {!r}'.format(member.name)
            )

    root = os.path.abspath(path)
    _makedirs(root)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    for member, parts, content in plan:
        target = os.path.join(root, *parts)
        if member.isdir():
            _makedirs(target)
            continue

        _makedirs(os.path.dirname(target))
        with os.fdopen(os.open(target, flags, 0o644), 'wb') as dst:
            if content is not None:
                dst.write(content)
            else:
                shutil.copyfileobj(tar.extractfile(member), dst)
