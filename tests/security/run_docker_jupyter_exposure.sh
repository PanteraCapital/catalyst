#!/bin/bash
# Run the Docker Jupyter exposure tests.
# They fail while the image still serves Jupyter as root on every interface
# with the documented password. A fix of that behavior should make them pass.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

VENV="${VENV:-/tmp/catalyst-nb56}"
PY_BOOT="${PY_BOOT:-}"

if ! "$VENV/bin/python" -c 'import notebook; assert notebook.__version__=="5.6.0"' >/dev/null 2>&1; then
  if [[ -z "$PY_BOOT" ]]; then
    if [[ -x "$HOME/.local/bin/uv" ]]; then
      PY_BOOT="$("$HOME/.local/bin/uv" python find 3.8)"
    elif command -v python3.8 >/dev/null 2>&1; then
      PY_BOOT="$(command -v python3.8)"
    else
      echo "python 3.8 is required to install notebook==5.6.0" >&2
      exit 1
    fi
  fi
  "$PY_BOOT" -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q 'pip==19.3.1' 'setuptools==39.2.0' 'wheel==0.31.1'
  "$VENV/bin/pip" install \
    notebook==5.6.0 \
    tornado==5.1 \
    traitlets==4.3.2 \
    ipython_genutils==0.2.0 \
    jupyter_core==4.4.0 \
    jupyter_client==5.2.3 \
    ipykernel==4.9.0 \
    ipython==6.5.0 \
    nbformat==4.4.0 \
    nbconvert==5.4.0 \
    pyzmq==17.1.2 \
    jinja2==2.10 \
    terminado==0.8.1 \
    prometheus_client==0.3.1 \
    Send2Trash==1.5.0 \
    'prompt_toolkit>=1.0.15,<2.0.0' \
    pexpect==4.6.0 \
    pygments==2.2.0 \
    backcall==0.1.0 \
    parso==0.3.1 \
    jedi==0.12.1 \
    pickleshare==0.7.4 \
    wcwidth==0.1.7 \
    decorator==4.3.0 \
    ptyprocess==0.6.0 \
    mistune==0.8.3 \
    entrypoints==0.2.3 \
    testpath==0.3.1 \
    pandocfilters==1.4.2 \
    bleach==2.1.4 \
    webencodings==0.5.1 \
    MarkupSafe==1.0 \
    python-dateutil==2.7.3 \
    six==1.11.0
fi

if [[ ! -d /projects ]]; then
  sudo mkdir -p /projects
  sudo chmod 1777 /projects
fi

export PYTHONWARNINGS=ignore
exec "$VENV/bin/python" -m unittest -v tests.security.test_docker_jupyter_exposure
