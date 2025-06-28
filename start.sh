#!/bin/bash

if [ ! -d "llama.cpp" ]; then
  # only run in dev env
  git clone https://github.com/ggerganov/llama.cpp
fi

export GGML_CUDA=OFF
if [[ -z "${RUN_LOCALLY}" ]]; then
  # enable CUDA if NOT running locally
  export GGML_CUDA=ON
fi

cd llama.cpp
cmake -B build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=${GGML_CUDA} -DLLAMA_CURL=OFF
cmake --build build --config Release -j 4 --target llama-quantize llama-gguf-split llama-imatrix
# Fentible: -j 4 works well for 16GB, but you can go down to -j 1 or 2 for even lower RAM, or increase for higher. Uncapped as -j (without a number) works for higher RAM.
cp ./build/bin/llama-* .
rm -rf build

cd ..
python gguf_repo_suite.py
