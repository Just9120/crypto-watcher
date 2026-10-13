#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
output=$1
compiler=${2:-gcc}
(cd vendor/sqlite && sha256sum --status -c SHA256SUMS)
(cd vendor/zstd && sha256sum --status -c SHA256SUMS)
"$compiler" -std=c99 -O3 -fPIC -shared \
  -I vendor/sqlite -I vendor/zstd/lib compressed_vfs.c \
  vendor/zstd/lib/common/*.c vendor/zstd/lib/decompress/*.c \
  vendor/zstd/lib/decompress/huf_decompress_amd64.S -o "$output"
