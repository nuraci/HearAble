#!/usr/bin/env bash
# Build the ASR runtime on the T9 Plus (Intel N95) itself, CPU only.
#
# Two builds come out of this script, and which one you get is the whole point:
#
#   NATIVE=off (default)  the baseline. AVX2, FMA and F16C named explicitly,
#                         no -march=native. This is what phases 3 to 6 measure.
#   NATIVE=on             the tuning variant of phase 8, which lets the compiler
#                         look at the machine and use everything it finds.
#
# The distinction is not cosmetic, and it is not obvious from ggml's options.
# ggml sets INS_ENB (which drives GGML_SSE42/AVX/AVX2/BMI2/FMA/F16C) to OFF when
# GGML_NATIVE is ON, because -march=native is expected to cover them; so asking
# for GGML_NATIVE=OFF is what turns the explicit flags ON. Getting this backwards
# produces a "baseline" with no AVX2 at all and a scalar kernel, which would make
# every number after it meaningless.
#
# The reference PC's historical RTF of 0.4664 came from a native build, so it is
# comparable with phase 8's variant, not with the baseline.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="${UPSTREAM:-$HERE/upstream/NeMo-Speech.cpp}"

# Same as scripts/build_cpu.sh: the runtime is a build tree, not source, so
# fetch it here at the revision every measurement was taken with.
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
NATIVE="${NATIVE:-off}"
case "$NATIVE" in
  on|ON|1)  GGML_NATIVE=ON;  SUFFIX=native ;;
  off|OFF|0) GGML_NATIVE=OFF; SUFFIX=baseline ;;
  *) echo "usage: NATIVE={on|off} $0" >&2; exit 2 ;;
esac
# Phase 8.3: the CPU declares avx_vnni and ggml's option for it is OFF by
# default. With GCC, GGML_AVX_VNNI=ON adds -mavxvnni, which makes the compiler
# define __AVXVNNI__, which is what selects _mm256_dpbusd_avx_epi32 inside
# mul_sum_us8_pairs_float — one instruction where the AVX2 path spends two.
VNNI="${VNNI:-off}"
case "$VNNI" in
  on|ON|1)  GGML_AVX_VNNI=ON;  SUFFIX="${SUFFIX}_vnni" ;;
  off|OFF|0) GGML_AVX_VNNI=OFF ;;
  *) echo "usage: VNNI={on|off} $0" >&2; exit 2 ;;
esac
BUILD_DIR="${BUILD_DIR:-$UPSTREAM/build/t9_cpu_$SUFFIX}"
DEPS_PREFIX="${DEPS_PREFIX:-$UPSTREAM/.deps-t9}"
SENTENCEPIECE_COMMIT="${SENTENCEPIECE_COMMIT:-17d7580d6407802f85855d2cc9190634e2c95624}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

echo "== host =="
uname -m; "${CXX:-c++}" --version | head -1; cmake --version | head -1
echo "== SIMD che il compilatore vede =="
grep -m1 '^flags' /proc/cpuinfo | tr ' ' '\n' | grep -E '^(sse4_1|sse4_2|avx|avx2|fma|f16c|bmi2|avx512f)$' | tr '\n' ' '; echo
echo "== variante: $SUFFIX (GGML_NATIVE=$GGML_NATIVE, GGML_AVX_VNNI=$GGML_AVX_VNNI) =="

if [ ! -f "$DEPS_PREFIX/sentencepiece/lib/libsentencepiece.a" ]; then
  echo "== sentencepiece, compilato qui =="
  WORK="$DEPS_PREFIX/sentencepiece-build"
  SOURCE="$WORK/source"
  SP_BUILD="$WORK/build"
  mkdir -p "$WORK"
  if [ ! -d "$SOURCE/.git" ]; then
    # The sources copied from the development machine are the same commit and
    # the same bytes; this machine's Wi-Fi is about 1 MB/s, so prefer them.
    if [ -d "$UPSTREAM/.deps/sentencepiece-build/source/.git" ]; then
      cp -a "$UPSTREAM/.deps/sentencepiece-build/source" "$SOURCE"
    else
      git clone --filter=blob:none --no-checkout \
        https://github.com/google/sentencepiece.git "$SOURCE"
    fi
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
  install -m 0644 "$SP_BUILD/src/libsentencepiece.a" \
    "$DEPS_PREFIX/sentencepiece/lib/libsentencepiece.a"
  cp -a "$SOURCE/src/"*.h "$DEPS_PREFIX/sentencepiece/include/" 2>/dev/null || true
  cp -a "$SOURCE/third_party/absl" "$DEPS_PREFIX/sentencepiece/include/" 2>/dev/null || true
fi

echo "== NeMo-Speech.cpp, ASR soltanto =="
cmake -S "$UPSTREAM" -B "$BUILD_DIR" -G "Unix Makefiles" \
  -DCMAKE_BUILD_TYPE=Release \
  -DNEMO_SPEECH_DEPENDENCY_PREFIX="$DEPS_PREFIX" \
  -DNEMO_SPEECH_GGML_PATCHED=OFF \
  -DGGML_NATIVE="$GGML_NATIVE" \
  -DGGML_AVX_VNNI="$GGML_AVX_VNNI" \
  -DGGML_CCACHE=OFF \
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
echo "== fatto =="
ls -l "$BUILD_DIR/bin" 2>/dev/null || true
