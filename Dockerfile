#
# Dockerfile for an image with the currently checked out version of catalyst installed. To build:
#
#    docker build -t enigmampc/catalyst .
#
# To run the container:
#
#    docker run -v /path/to/your/notebooks:/projects -v ~/.catalyst:/root/.catalyst -p 8888:8888/tcp --name catalyst -it enigmampc/catalyst
#
# To access Jupyter when running docker locally (you may need to add NAT rules):
#
#    https://127.0.0.1:8888      <- Please note HTTPS, not HTTP
#
# Default password is 'jupyter'. To provide another, see:
#    http://jupyter-notebook.readthedocs.org/en/latest/public_server.html#preparing-a-hashed-password
#
# Once generated, you can pass the new value via `docker run --env` the first time
# you start the container.
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
    NOTEBOOK_PORT=8888 \
    SSL_CERT_PEM=/root/.jupyter/jupyter.pem \
    SSL_CERT_KEY=/root/.jupyter/jupyter.key \
    PW_HASH="u'sha1:31cb67870a35:1a2321318481f00b0efdf3d1f71af523d3ffc505'" \
    CONFIG_PATH=/root/.jupyter/jupyter_notebook_config.py

#
# install TA-Lib and other prerequisites
#

COPY ./etc/install_talib.sh /tmp/install_talib.sh

RUN mkdir ${PROJECT_DIR} \
    && apt-get -y update \
    && apt-get -y install libfreetype6-dev libpng-dev libopenblas-dev liblapack-dev gfortran \
    && bash /tmp/install_talib.sh /usr \
    && rm /tmp/install_talib.sh

#
# build and install catalyst from source.  install TA-Lib after to ensure
# numpy is available.
#

RUN pip install 'numpy==1.14.0' \
  && pip install 'scipy==1.0.0' \
  && pip install 'pandas==0.19.2' \
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

WORKDIR ${PROJECT_DIR}
CMD /docker_cmd.sh
