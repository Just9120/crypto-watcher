#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
output=$1
compiler=${2:-gcc}
python_include=${3:?Exact runtime Python include directory required}
test -f "$python_include/Python.h"
test -f "$python_include/pyconfig.h"
"$compiler" -std=c99 -O3 -ffp-contract=off -fPIC -shared -fvisibility=hidden \
  -Wall -Wextra -Werror -pedantic -isystem "$python_include" -I vendor/yyjson \
  frozen_codec.c vendor/yyjson/yyjson.c -lm -o "$output"
