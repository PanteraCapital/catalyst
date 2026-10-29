import os
import json
import tarfile

from six import string_types

from catalyst.constants import SUPPORTED_WALLETS
from catalyst.utils.deprecate import deprecated
from catalyst.utils.paths import data_root, ensure_directory
from catalyst.marketplace.marketplace_errors import MarketplaceJSONError, \
    MarketplaceInvalidName

# Entries of the marketplace folder that are not datasets.
RESERVED_MARKETPLACE_NAMES = ('temp_bundles', 'addresses.json')


def _child_path(parent, name, reserved=()):
    """
    Join a single untrusted path component under ``parent``.

    Dataset names come from the marketplace contract and file names from
    the ingest server, so anything that could resolve to a location other
    than a direct child of ``parent`` is rejected before the filesystem is
    touched.

    Parameters
    ----------
    parent: str
    name: str
    reserved: tuple[str]

    Returns
    -------
    str

    """
    if not isinstance(name, string_types) or not name \
            or name.startswith('.') or name in reserved or '\0' in name \
            or any(sep and sep in name
                   for sep in ('/', '\\', os.sep, os.altsep)) \
            or os.path.isabs(name) or os.path.splitdrive(name)[0]:
        raise MarketplaceInvalidName(name=name)

    path = os.path.join(parent, name)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(parent):
        raise MarketplaceInvalidName(name=name)

    return path


def get_marketplace_folder(environ=None):
    """
    The root path of the marketplace folder.

    Parameters
    ----------
    environ:

    Returns
    -------
    str

    """
    if not environ:
        environ = os.environ

    root = data_root(environ)
    marketplace_folder = os.path.join(root, 'marketplace')
    ensure_directory(marketplace_folder)

    return marketplace_folder


def get_data_source_folder(data_source_name, environ=None):
    """
    The root path of an data_source folder.

    Parameters
    ----------
    data_source_name: str
    environ:

    Returns
    -------
    str

    """
    data_source_folder = get_data_source_path(data_source_name, environ)
    ensure_directory(data_source_folder)

    return data_source_folder


def get_data_source_path(data_source_name, environ=None):
    """
    The path of a data_source folder, without creating it.

    Parameters
    ----------
    data_source_name: str
    environ:

    Returns
    -------
    str

    """
    return _child_path(
        get_marketplace_folder(environ),
        data_source_name,
        RESERVED_MARKETPLACE_NAMES,
    )


@deprecated
def get_bundle_folder(data_source_name, data_frequency, environ=None):
    data_source_folder = get_data_source_folder(data_source_name, environ)

    bundle_folder = _child_path(data_source_folder, data_frequency)

    ensure_directory(bundle_folder)

    return bundle_folder


def get_temp_bundles_folder(environ=None):
    """
    The temp folder for bundle downloads by algo name.

    Parameters
    ----------
    ds_name: str
    environ:

    Returns
    -------
    str

    """
    root = data_root(environ)
    folder = os.path.join(root, 'marketplace', 'temp_bundles')
    ensure_directory(folder)

    return folder


def get_temp_bundle_path(filename, environ=None):
    """
    The path of a downloaded file inside the temp bundles folder.

    Parameters
    ----------
    filename: str
    environ:

    Returns
    -------
    str

    """
    return _child_path(get_temp_bundles_folder(environ), filename)


def extract_bundle(tar_filename):
    """
    Extract a bcolz bundle.

    Only regular files and directories that stay inside the target folder
    are accepted.

    Parameters
    ----------
    ds_name

    Returns
    -------
    str

    """
    target_path = tar_filename.replace('.tar.gz', '')
    root = os.path.realpath(target_path)
    with tarfile.open(tar_filename, 'r') as tar:
        members = tar.getmembers()
        for member in members:
            dest = os.path.realpath(os.path.join(root, member.name))
            if not (member.isfile() or member.isdir()) or \
                    (dest != root and not dest.startswith(root + os.sep)):
                raise MarketplaceInvalidName(name=member.name)

        if hasattr(tarfile, 'data_filter'):
            tar.extractall(target_path, members=members, filter='data')
        else:
            tar.extractall(target_path, members=members)

    return target_path


def get_user_pubaddr(environ=None):
    """
    The de-serialized content of the user's addresses.json file.

    Parameters
    ----------
    environ:

    Returns
    -------
    Object

    """
    marketplace_folder = get_marketplace_folder(environ)
    filename = os.path.join(marketplace_folder, 'addresses.json')

    if os.path.isfile(filename):
        with open(filename) as data_file:
            try:
                data = json.load(data_file)
            except json.decoder.JSONDecodeError as e:
                raise MarketplaceJSONError(file=filename, error=e)
            try:
                d = data[0]['pubAddr']
            except Exception as e:
                data = [data, ]

            changed = False

            for idx, d in enumerate(data):
                try:
                    if d['wallet'] not in SUPPORTED_WALLETS:
                        data[idx]['wallet'] = _choose_wallet(
                            d['pubAddr'], False)
                        changed = True
                except KeyError:
                    data[idx]['wallet'] = _choose_wallet(
                        d['pubAddr'], True)
                    changed = True

            if changed:
                save_user_pubaddr(data)

            return data

    else:
        data = []
        data.append(dict(pubAddr='', desc='', wallet='', accepted_terms=False))
        with open(filename, 'w') as f:
            json.dump(data, f, sort_keys=False, indent=2,
                      separators=(',', ':'))
            return data


def _choose_wallet(pubAddr, missing):
    while True:
        if missing:
            print('\nYou need to specify a wallet for address '
                  '{}.'.format(pubAddr))
        else:
            print('\nThe wallet specified for address {} is not '
                  'supported.'.format(pubAddr))

        print('Please choose among the following options:')
        for idx, wallet in enumerate(SUPPORTED_WALLETS):
            print('{}\t{}'.format(idx, wallet))

        lw = len(SUPPORTED_WALLETS) - 1
        w = input('Choose a number between 0 and {}: '.format(lw))
        try:
            w = int(w)
        except ValueError:
            print('Enter a number between 0 and {}'.format(lw))
        else:
            if w not in range(0, lw + 1):
                print('Enter a number between 0 and '
                      '{}'.format(lw))
            else:
                return SUPPORTED_WALLETS[w]


def save_user_pubaddr(data, environ=None):
    """
    Saves the user's public addresses and their related metadata in
    the corresponding addresses.json file.

    Parameters
    ----------
    data: dict

    Returns
    -------
    True

    """
    marketplace_folder = get_marketplace_folder(environ)
    filename = os.path.join(marketplace_folder, 'addresses.json')

    with open(filename, 'w') as f:
        json.dump(data, f, sort_keys=False, indent=2,
                  separators=(',', ':'))

    return True
