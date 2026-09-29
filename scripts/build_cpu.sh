#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${UPSTREAM:-$ROOT/upstream/NeMo-Speech.cpp}"
BUILD_DIR="${BUILD_DIR:-$UPSTREAM/build/cpu-asr-make}"
DEPS_PREFIX="${DEPS_PREFIX:-$UPSTREAM/.deps}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
SENTENCEPIECE_COMMIT="${SENTENCEPIECE_COMMIT:-17d7580d6407802f85855d2cc9190634e2c95624}"

# The runtime is a build tree, not source, so it is not carried in this
# repository. Reproduce it here at the revision every measurement was taken
# with — a fresh clone could not build without this, which is exactly what a
# first build from a clean checkout found.
NEMO_REPO="${NEMO_REPO:-https://github.com/NVIDIA/NeMo-Speech.cpp.git}"
NEMO_COMMIT="${NEMO_COMMIT:-4f9676226f667d14608487df744f375db87127f8}"
if [ ! -d "$UPSTREAM/.git" ]; then
  echo "fetching the ASR runtime at ${NEMO_COMMIT:0:7} into $UPSTREAM"
  mkdir -p "$(dirname "$UPSTREAM")"
  # --no-checkout first: the upstream repository declares LFS filters, and a
  # clone that checks out immediately invokes git-lfs before anything can turn
  # it off. On a machine without git-lfs that aborts the checkout, which is how
  # a first build from a clean clone fails — the filters have to be disabled in
  # between.
  git clone --filter=blob:none --no-checkout "$NEMO_REPO" "$UPSTREAM"
  git -C "$UPSTREAM" config filter.lfs.process ''
  git -C "$UPSTREAM" config filter.lfs.clean ''
  git -C "$UPSTREAM" config filter.lfs.smudge ''
  git -C "$UPSTREAM" config filter.lfs.required false
  git -C "$UPSTREAM" checkout --quiet "$NEMO_COMMIT"
fi

git -C "$UPSTREAM" config filter.lfs.process ''
git -C "$UPSTREAM" config filter.lfs.clean ''
git -C "$UPSTREAM" config filter.lfs.smudge ''
git -C "$UPSTREAM" config filter.lfs.required false
git -C "$UPSTREAM" submodule update --init ggml

if [ ! -f "$DEPS_PREFIX/sentencepiece/lib/libsentencepiece.a" ] ||
   [ ! -f "$DEPS_PREFIX/sentencepiece/include/sentencepiece_processor.h" ]; then
  WORK="$DEPS_PREFIX/sentencepiece-build"
  SOURCE="$WORK/source"
  SP_BUILD="$WORK/build"
  mkdir -p "$WORK"
  if [ ! -d "$SOURCE/.git" ]; then
    git clone --filter=blob:none --no-checkout https://github.com/google/sentencepiece.git "$SOURCE"
  fi
  git -C "$SOURCE" fetch --depth 1 origin "$SENTENCEPIECE_COMMIT"
  git -C "$SOURCE" checkout --quiet "$SENTENCEPIECE_COMMIT"
  cmake -G "Unix Makefiles" -S "$SOURCE" -B "$SP_BUILD" \
    -DCMAKE_BUILD_TYPE=Release \
    -DSPM_BUILD_TEST=OFF \
    -DSPM_ENABLE_SHARED=OFF \
    -DSPM_ENABLE_TCMALLOC=OFF
  cmake --build "$SP_BUILD" --target sentencepiece-static -j "$JOBS"
  install -d "$DEPS_PREFIX/sentencepiece/lib" "$DEPS_PREFIX/sentencepiece/include"
  install -m 0644 "$SP_BUILD/src/libsentencepiece.a" "$DEPS_PREFIX/sentencepiece/lib/libsentencepiece.a"
  install -m 0644 "$SOURCE/src/sentencepiece_processor.h" "$DEPS_PREFIX/sentencepiece/include/sentencepiece_processor.h"
fi

cmake -S "$UPSTREAM" -B "$BUILD_DIR" -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release \
  -DNEMO_SPEECH_DEPENDENCY_PREFIX="$DEPS_PREFIX" \
  -DNEMO_SPEECH_GGML_PATCHED=OFF \
  -DNEMO_SPEECH_BUILD_ASR=ON \
  -DNEMO_SPEECH_BUILD_DIAR=OFF \
  -DNEMO_SPEECH_BUILD_TTS=OFF \
  -DNEMO_SPEECH_BUILD_NMT=OFF \
  -DNEMO_SPEECH_BUILD_CLI=ON \
  -DNEMO_SPEECH_BUILD_MIC_CAPTURE=OFF \
  -DNEMO_SPEECH_BUILD_HTTP=OFF \
  -DNEMO_SPEECH_BUILD_GRPC=OFF \
  -DNEMO_SPEECH_BUILD_TESTS=OFF \
  -DNEMO_SPEECH_BUILD_EXAMPLES=OFF \
  -DNEMO_SPEECH_BUILD_TOOLS=OFF

cmake --build "$BUILD_DIR" -j "$JOBS"

echo "$BUILD_DIR/bin/nemo-speech"

