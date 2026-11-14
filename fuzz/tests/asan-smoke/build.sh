#!/bin/bash
# Build the ASAN+PCGUARD SUT and the non-ASAN LLVM-coverage replay binary.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
AFL="$ROOT/.deps/AFLplusplus"
CC="$AFL/afl-clang-fast"
SRC="$HERE/server.c"
BUILD="$HERE/build"

if [[ ! -x "$CC" ]]; then
  echo "afl-clang-fast is missing at $CC" >&2
  exit 1
fi
for tool in clang-18 llvm-profdata-18 llvm-cov-18; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required" >&2
    exit 1
  fi
done

rm -rf "$BUILD"
mkdir -p "$BUILD"

# ASAN + PCGUARD: the fuzzing target. AFL_USE_ASAN makes the AFL runtime and
# the clang driver link the ASAN allocator/runtime.
AFL_PATH="$AFL" AFL_LLVM_INSTRUMENT=PCGUARD AFL_USE_ASAN=1 \
  "$CC" -std=c11 -O1 -g -fno-omit-frame-pointer -fsanitize=address \
  "$SRC" -o "$BUILD/asan-server"

# Forkserver-only LLVM coverage binary: no ASAN and no PCGUARD counters; the
# profile flags produce indexed source coverage for mcn-fuzz coverage.
AFL_PATH="$AFL" AFL_LLVM_ONLY_FSRV=1 \
  "$CC" -std=c11 -O1 -g \
  -fprofile-instr-generate -fcoverage-mapping -fprofile-update=atomic \
  -mllvm -runtime-counter-relocation \
  "$SRC" -o "$BUILD/cov-server"

printf 'built %s\n' "$BUILD/asan-server"
printf 'built %s\n' "$BUILD/cov-server"
