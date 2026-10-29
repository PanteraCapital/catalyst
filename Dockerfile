#
# Dockerfile for an image with the currently checked out version of catalyst installed. To build:
#
#    docker build -t enigmampc/catalyst .
#
# Jupyter runs as the unprivileged user 'catalyst' (uid 1000) and requires a token or a
# hashed password supplied by you. To run the container:
#
#    export NOTEBOOK_TOKEN="$(openssl rand -hex 32)"
#    docker run -v /path/to/your/notebooks:/projects -v ~/.catalyst:/root/.catalyst -e NOTEBOOK_TOKEN -e NOTEBOOK_IP=0.0.0.0 -p 127.0.0.1:8888:8888/tcp --name catalyst -it enigmampc/catalyst
#
# The mounted directories must be writable by uid 1000. NOTEBOOK_IP=0.0.0.0 makes Jupyter
# listen inside the container so that docker can forward the port; without it Jupyter only
# listens on the container's loopback interface. The -p flag above publishes the port on
# the host's loopback interface only.
#
# To access Jupyter when running docker locally:
#
#    https://127.0.0.1:8888      <- Please note HTTPS, not HTTP
#
# and log in with the value of NOTEBOOK_TOKEN. To use a password instead, pass a hashed
# password as NOTEBOOK_PASSWORD_HASH, see:
#    http://jupyter-notebook.readthedocs.org/en/latest/public_server.html#preparing-a-hashed-password
#
# You can also run an algo using the docker exec command. For example:
#
#    docker exec -it catalyst catalyst run -f /projects/my_algo.py --start 2015-1-1 --end 2016-1-1 /projects/result.pickle
#
FROM python:3.6

#
# set up environment
#
ENV TINI_VERSION v0.10.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini
ENTRYPOINT ["/tini", "--"]

ENV PROJECT_DIR=/projects \
    NOTEBOOK_IP=127.0.0.1 \
    NOTEBOOK_PORT=8888 \
    SSL_CERT_PEM=/root/.jupyter/jupyter.pem \
    SSL_CERT_KEY=/root/.jupyter/jupyter.key \
    CONFIG_PATH=/root/.jupyter/jupyter_notebook_config.py

#
# install TA-Lib and other prerequisites. The unprivileged 'catalyst' user
# keeps /root as its home so that the /root/.catalyst and /root/.jupyter
# paths above stay valid.
#

RUN useradd --uid 1000 --user-group --home-dir /root --no-create-home --shell /bin/bash catalyst \
    && mkdir ${PROJECT_DIR} \
    && apt-get -y update \
    && apt-get -y install libfreetype6-dev libpng-dev libopenblas-dev liblapack-dev gfortran \
    && curl -L https://downloads.sourceforge.net/project/ta-lib/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz | tar xvz

#
# build and install catalyst from source.  install TA-Lib after to ensure
# numpy is available.
#

WORKDIR /ta-lib

RUN pip install 'numpy==1.14.0' \
  && pip install 'scipy==1.0.0' \
  && pip install 'pandas==0.19.2' \
  && ./configure --prefix=/usr \
  && make \
  && make install \
  && pip install TA-Lib \
  && pip install matplotlib \
  && pip install jupyter

#
# This is then only file we need from source to remain in the
# image after build and install.
#

ADD ./etc/docker_cmd.sh /

#
# make port available. /catalyst is made a volume
# for developer testing.
#
EXPOSE ${NOTEBOOK_PORT}

#
# build and install the catalyst package into the image
#

ADD . /catalyst
WORKDIR /catalyst
RUN pip install -e .

#
# start the jupyter server
#

RUN chown -R catalyst:catalyst /root ${PROJECT_DIR}
USER catalyst
WORKDIR ${PROJECT_DIR}
CMD /docker_cmd.sh
