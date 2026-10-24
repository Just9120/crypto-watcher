"""Focused public vendor API regressions; first-party Linux builds remain mandatory."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "native/vendor"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Canonical Linux C compiler/sanitizers")


def compile_run(tmp_path, code, sources, includes):
    compiler = shutil.which("gcc")
    assert compiler, "Required Linux compiler unavailable"
    path = tmp_path / "regression.c"
    path.write_text(code, encoding="utf-8")
    output = tmp_path / "regression"
    subprocess.run(
        [
            compiler,
            "-std=c99",
            "-O1",
            "-g",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-DZSTD_DISABLE_ASM",
            *["-I" + str(p) for p in includes],
            str(path),
            *map(str, sources),
            "-lm",
            "-o",
            str(output),
        ],
        check=True,
        timeout=120,
    )
    subprocess.run([str(output)], check=True, timeout=20, capture_output=True)


def function(source, name):
    start = source.index("static ", source.rfind("\n}", 0, source.index(name)) + 2)
    brace = source.index("{", source.index(name, start))
    end = brace + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def test_zstd_ddict_collision_wrap_and_bounded_absent_lookup(tmp_path):
    source = (VENDOR / "zstd/lib/decompress/zstd_decompress.c").read_text()
    # Compile the actual private helpers with a deterministic hash seam. No copied probe logic.
    prefix = r"""
        #include <assert.h>
        #include <stddef.h>
        #include <stdint.h>
        typedef uint32_t U32; typedef uint64_t U64;
        typedef struct { U32 id; } ZSTD_DDict;
        typedef struct { const ZSTD_DDict** ddictPtrTable; size_t ddictPtrTableSize;
                         size_t ddictPtrCount; } ZSTD_DDictHashSet;
        static U64 XXH64(const void* p, size_t n, int seed) { (void)n; (void)seed; return *(const U32*)p; }
        static U32 ZSTD_getDictID_fromDDict(const ZSTD_DDict* p) { return p ? p->id : 0; }
        #define DEBUGLOG(...) ((void)0)
        #define RETURN_ERROR_IF(c, ...) do { if(c) return (size_t)-1; } while(0)
    """
    functions = "\n".join(
        function(source, name)
        for name in (
            "ZSTD_DDictHashSet_getIndex(",
            "ZSTD_DDictHashSet_emplaceDDict(",
            "ZSTD_DDictHashSet_getDDict(",
        )
    )
    main = r"""
        int main(void) {
            const ZSTD_DDict* slots[4] = {0};
            ZSTD_DDictHashSet set = {slots, 4, 0};
            ZSTD_DDict a={3}, b={7}, replacement={7}, c={11}, d={15}, missing={19};
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&a)==0);
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&b)==0);
            assert(slots[3]==&a && slots[0]==&b);
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&replacement)==0 && set.ddictPtrCount==2);
            assert(ZSTD_DDictHashSet_getDDict(&set,7)==&replacement);
            assert(ZSTD_DDictHashSet_getDDict(&set,19)==NULL);
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&c)==0);
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&d)==0);
            assert(ZSTD_DDictHashSet_getDDict(&set,19)==NULL);
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&missing)!=0);
            set.ddictPtrCount=3;
            assert(ZSTD_DDictHashSet_emplaceDDict(&set,&missing)!=0);
            return 0;
        }
    """
    compile_run(tmp_path, prefix + functions + main, [], [])


def test_yyjson_wrapped_ranges_and_file_stream_compatibility(tmp_path):
    code = r"""
        #define _GNU_SOURCE
        #include "yyjson.h"
        #include <assert.h>
        #include <stdint.h>
        #include <stdio.h>
        #include <string.h>
        #include <unistd.h>
        #include <limits.h>
        int main(void) {
            yyjson_mut_doc* d=yyjson_mut_doc_new(NULL);
            yyjson_mut_val* a=yyjson_mut_arr(d);
            assert(yyjson_mut_arr_append(a,yyjson_mut_int(d,1)));
            assert(yyjson_mut_arr_append(a,yyjson_mut_int(d,2)));
            assert(!yyjson_mut_arr_remove_range(a,SIZE_MAX,2));
            assert(!yyjson_mut_arr_remove_range(a,1,SIZE_MAX));
            assert(yyjson_mut_arr_size(a)==2 && yyjson_mut_get_int(yyjson_mut_arr_get(a,0))==1);
            assert(yyjson_mut_arr_remove_range(a,2,0));
            assert(yyjson_mut_arr_remove_range(a,0,1));
            assert(yyjson_mut_get_int(yyjson_mut_arr_get(a,0))==2);
            assert(yyjson_mut_arr_remove_range(a,0,1) && yyjson_mut_arr_size(a)==0);
            yyjson_mut_doc_free(d);
            yyjson_read_err error;
            FILE* fp=tmpfile(); assert(fp);
            assert(fputs("xx{\"ok\":true}",fp)>=0); assert(fseek(fp,2,SEEK_SET)==0);
            yyjson_doc* parsed=yyjson_read_fp(fp,0,NULL,&error); assert(parsed);
            assert(yyjson_get_bool(yyjson_obj_get(yyjson_doc_get_root(parsed),"ok")));
            yyjson_doc_free(parsed); fclose(fp);
            int pipefd[2]; assert(pipe(pipefd)==0);
            assert(write(pipefd[1],"{\"ok\":true}",11)==11); close(pipefd[1]);
            fp=fdopen(pipefd[0],"r"); assert(fp);
            parsed=yyjson_read_fp(fp,0,NULL,&error); assert(parsed);
            yyjson_doc_free(parsed); fclose(fp);
            return 0;
        }
    """
    compile_run(tmp_path, code, [VENDOR / "yyjson/yyjson.c"], [VENDOR / "yyjson"])
    # LLP64-sensitive boundary is the real source accumulator type, in addition to executable controls.
    body = (VENDOR / "yyjson/yyjson.c").read_text().split("yyjson_doc *yyjson_read_fp(", 1)[1]
    assert "usize file_size = 0;" in body and "file_size += (long)read_size" not in body


def test_fastcover_small_training_partition_is_rejected_before_dmer_count(tmp_path):
    directory = VENDOR / "zstd/lib"
    code = r"""
        #include "dictBuilder/fastcover.c"
        #include <assert.h>
        int main(void) {
            FASTCOVER_ctx_t ctx;
            unsigned char samples[128]={0};
            size_t sizes[6]={1,1,1,1,1,100};
            FASTCOVER_accel_t acceleration={100,0};
            memset(&ctx,0,sizeof(ctx));
            assert(ZSTD_isError(FASTCOVER_ctx_init(&ctx,samples,sizes,6,8,0.9,10,acceleration)));
            assert(ctx.offsets==NULL && ctx.freqs==NULL);
            sizes[0]=4; /* Exactly eight training bytes; testing still present. */
            assert(FASTCOVER_ctx_init(&ctx,samples,sizes,6,8,0.9,10,acceleration)==0);
            assert(ctx.nbDmers==1);
            FASTCOVER_ctx_destroy(&ctx);
            return 0;
        }
    """
    sources = [
        p
        for folder in ("common", "compress", "decompress", "dictBuilder")
        for p in (directory / folder).glob("*.c")
        if p.name != "fastcover.c"
    ]
    compile_run(tmp_path, code, sources, [directory])
