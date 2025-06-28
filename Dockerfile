FROM nvidia/cuda:12.8.0-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends --fix-missing \
    git \
    git-lfs \
    wget \
    curl \
    cmake \
    # python build dependencies \
    build-essential \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    libncursesw5-dev \
    xz-utils \
    tk-dev \
    libxml2-dev \
    libxmlsec1-dev \
    libffi-dev \
    liblzma-dev \
    ffmpeg

# Check if user with UID 1000 exists, if not create it
RUN id -u 1000 &>/dev/null || useradd -m -u 1000 user
USER 1000
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:${PATH}
WORKDIR ${HOME}/app

RUN curl https://pyenv.run | bash
ENV PATH=${HOME}/.pyenv/shims:${HOME}/.pyenv/bin:${PATH}
ARG PYTHON_VERSION=3.11
RUN pyenv install ${PYTHON_VERSION} && \
    pyenv global ${PYTHON_VERSION} && \
    pyenv rehash && \
    pip install --no-cache-dir -U pip setuptools wheel && \
    pip install "huggingface-hub" "hf-transfer" "gradio[oauth]" "gradio_huggingfacehub_search" "APScheduler"

COPY --chown=1000 . ${HOME}/app
RUN git clone https://github.com/ggerganov/llama.cpp
RUN pip install -r llama.cpp/requirements/requirements-convert_hf_to_gguf.txt

COPY groups_merged.txt ${HOME}/app/llama.cpp/

ENV PYTHONPATH=${HOME}/app \
    PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    GRADIO_ALLOW_FLAGGING=never \
    GRADIO_NUM_PORTS=1 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_THEME=huggingface \
    TQDM_POSITION=-1 \
    TQDM_MININTERVAL=1 \
    SYSTEM=spaces \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH} \
    PATH=/usr/local/nvidia/bin:${PATH}

ENTRYPOINT /bin/bash start.sh

