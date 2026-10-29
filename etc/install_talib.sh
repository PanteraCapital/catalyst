#!/usr/bin/env bash
#
# Download, verify, build and install the TA-Lib C library.
#
# Usage: install_talib.sh [PREFIX]    (PREFIX defaults to /usr)
#
# The archive is checked against a pinned SHA-256 before it is unpacked, and
# only that checked copy is built. The build runs as the invoking user; sudo is
# used for "make install" only when PREFIX is not writable by that user.

set -euo pipefail

TALIB_SRC_URL="https://downloads.sourceforge.net/project/ta-lib/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz"
TALIB_SRC_SHA256="9ff41efcb1c011a4b4b6dfc91610b06e39b1d7973ed5d4dee55029a0ac4dc651"

prefix="${1:-/usr}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
archive="$workdir/ta-lib-0.4.0-src.tar.gz"

curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --output "$archive" "$TALIB_SRC_URL"

if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$archive" | cut -d ' ' -f 1)"
else
    actual_sha256="$(shasum -a 256 "$archive" | cut -d ' ' -f 1)"
fi
if [ "$actual_sha256" != "$TALIB_SRC_SHA256" ]; then
    echo "TA-Lib archive checksum mismatch:" >&2
    echo "  expected $TALIB_SRC_SHA256" >&2
    echo "  got      $actual_sha256" >&2
    exit 1
fi

tar -xzf "$archive" -C "$workdir"
cd "$workdir/ta-lib"
./configure --prefix="$prefix"
make
if [ -w "$prefix" ]; then
    make install
else
    sudo make install
fi
