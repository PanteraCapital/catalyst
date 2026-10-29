#!/bin/bash
#
# Start the Jupyter notebook server of the catalyst image.
#
# The operator must supply the notebook credentials through the environment:
#
#    NOTEBOOK_TOKEN           a long random token, and/or
#    NOTEBOOK_PASSWORD_HASH   a hashed password, see
#                             http://jupyter-notebook.readthedocs.org/en/latest/public_server.html#preparing-a-hashed-password
#
# The server listens on NOTEBOOK_IP (127.0.0.1 unless configured otherwise).
#
set -eu

NOTEBOOK_IP=${NOTEBOOK_IP:-127.0.0.1}
NOTEBOOK_PORT=${NOTEBOOK_PORT:-8888}
NOTEBOOK_TOKEN=${NOTEBOOK_TOKEN:-}
NOTEBOOK_PASSWORD_HASH=${NOTEBOOK_PASSWORD_HASH:-}

umask 077

#
# generate configuration and cert if they do not exist yet
#
AUTH_MARKER='# catalyst: notebook credentials come from the environment'
mkdir -p "$(dirname "${CONFIG_PATH}")"
if ! grep -qxF "${AUTH_MARKER}" "${CONFIG_PATH}" 2>/dev/null ; then
    # Appended last so that it overrides any password set earlier in an
    # existing config, such as the one previous images baked in.
    cat >> "${CONFIG_PATH}" <<EOF
${AUTH_MARKER}
import os
c.NotebookApp.password = os.environ.get('NOTEBOOK_PASSWORD_HASH', '')
if os.environ.get('NOTEBOOK_TOKEN'):
    c.NotebookApp.token = os.environ['NOTEBOOK_TOKEN']
EOF
fi

if [ ! -f "${SSL_CERT_PEM}" ] || [ ! -f "${SSL_CERT_KEY}" ] ; then
    mkdir -p "$(dirname "${SSL_CERT_PEM}")" "$(dirname "${SSL_CERT_KEY}")"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -subj "/C=US/ST=Denial/L=Springfield/O=Dis/CN=127.0.0.1" \
        -keyout "${SSL_CERT_KEY}" -out "${SSL_CERT_PEM}"
fi

if [ "$(id -u)" -eq 0 ] ; then
    echo "Refusing to run Jupyter as root. Run the container as an" \
         "unprivileged user (the image default is 'catalyst')." >&2
    exit 1
fi

if [ -z "${NOTEBOOK_TOKEN}" ] && [ -z "${NOTEBOOK_PASSWORD_HASH}" ] ; then
    echo "Refusing to start Jupyter without credentials. Pass NOTEBOOK_TOKEN" \
         "or NOTEBOOK_PASSWORD_HASH with 'docker run --env'." >&2
    exit 1
fi

exec jupyter notebook --no-browser --notebook-dir="${PROJECT_DIR}" \
    --certfile="${SSL_CERT_PEM}" --keyfile="${SSL_CERT_KEY}" \
    --ip="${NOTEBOOK_IP}" --port="${NOTEBOOK_PORT}" \
    --config="${CONFIG_PATH}"
