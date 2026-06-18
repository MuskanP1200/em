ARG BASE_IMAGE=acrcentralusvodev.azurecr.io/base/python:3.12.3-slim
FROM ${BASE_IMAGE} AS pythoninstalled

WORKDIR /app

RUN groupadd app \
    && useradd -r -s /bin/false -N -g app app

RUN chown app:app /app

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt upgrade -y && rm -rf /var/lib/apt/lists/*

#TODO: activate egress to debian registry
# RUN apt-get update && \
#     apt-get install -y --no-install-recommends gcc python3-dev build-essential && \
#     rm -rf /var/lib/apt/lists/*

# COPY ./utils/ /utils
COPY ./backend/requirements/requirements.txt /build/backend/requirements.txt

ENV VENV_PATH=/opt/venv 
ENV PATH=$VENV_PATH/bin:$PATH
ENV PIP_CACHE_DIR=/var/cache/pip XDG_CACHE_HOME=/var/cache/

RUN --mount=type=cache,target=/var/cache/pip \
    --mount=type=secret,id=pip_conf,target=/etc/pip.conf,required=true \
    python -m venv $VENV_PATH && \
    $VENV_PATH/bin/python -m pip install --upgrade pip && \
    $VENV_PATH/bin/python -m pip install --cache-dir /var/cache/pip -r /build/backend/requirements.txt

COPY ./backend/ /app/

EXPOSE 8018

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8018"]