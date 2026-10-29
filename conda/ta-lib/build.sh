#!/bin/bash
set -euo pipefail

TALIB_SRC_URL="https://downloads.sourceforge.net/project/ta-lib/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz"
TALIB_SRC_SHA256="9ff41efcb1c011a4b4b6dfc91610b06e39b1d7973ed5d4dee55029a0ac4dc651"

talib_dir="$(mktemp -d)"
talib_archive="$talib_dir/ta-lib-0.4.0-src.tar.gz"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --output "$talib_archive" "$TALIB_SRC_URL"
if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$talib_archive" | cut -d ' ' -f 1)"
else
    actual_sha256="$(shasum -a 256 "$talib_archive" | cut -d ' ' -f 1)"
fi
if [ "$actual_sha256" != "$TALIB_SRC_SHA256" ]; then
    echo "TA-Lib archive checksum mismatch: got $actual_sha256" >&2
    rm -rf "$talib_dir"
    exit 1
fi
tar -xzf "$talib_archive" -C "$talib_dir"
pushd "$talib_dir/ta-lib"
./configure --prefix="$PREFIX"
make
make install
popd
rm -rf "$talib_dir"

export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}:$PREFIX/lib
export TA_INCLUDE_PATH=$PREFIX/include
export TA_LIBRARY_PATH=$PREFIX/lib
python setup.py build
python setup.py install --prefix=$PREFIX
