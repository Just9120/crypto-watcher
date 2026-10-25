#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
output=$1
compiler=${2:-gcc}
"$compiler" -std=c99 -O3 -ffp-contract=off -fPIC -shared -fvisibility=hidden \
  -Wall -Wextra -Werror -pedantic -I vendor/yyjson \
  source_guard.c vendor/yyjson/yyjson.c -lm -o "$output"
