#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
output=$1
compiler=${2:-gcc}
(cd vendor/sqlite && sha256sum --status -c SHA256SUMS)
(cd vendor/zstd && sha256sum --status -c SHA256SUMS)
temporary=$(mktemp -d /tmp/cw-numeric-XXXXXXXX)
cleanup() {
  case "$temporary" in /tmp/cw-numeric-*) rm -r -- "$temporary";; *) return 1;; esac
}
trap cleanup EXIT
for source in numeric_frame numeric_frame_proof; do
  "$compiler" -std=c99 -O3 -fPIC -fvisibility=hidden -Wall -Wextra -Werror -pedantic \
    -I vendor/sqlite -I vendor/zstd/lib -c "$source.c" -o "$temporary/$source.o"
done
"$compiler" -std=c99 -O3 -fPIC -shared -fvisibility=hidden -DZSTD_DISABLE_ASM=1 \
  -DZSTD_LEGACY_SUPPORT=0 -I vendor/zstd/lib \
  "$temporary/numeric_frame.o" "$temporary/numeric_frame_proof.o" \
  vendor/zstd/lib/common/*.c vendor/zstd/lib/decompress/*.c -o "$output"
