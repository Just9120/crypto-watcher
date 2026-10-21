#define _POSIX_C_SOURCE 200809L
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT1
#include "zstd.h"
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <limits.h>
#include <math.h>

#define BLOCK 65536u
#define SLOTS 512u
typedef struct { uint64_t off, len; } Frame;
typedef struct { uint64_t block, age; } Slot;
typedef struct Ctx Ctx;
typedef struct { sqlite3_file base; Ctx *ctx; sqlite3_file *parent; } File;
struct Ctx {
    sqlite3_vfs vfs; sqlite3_vfs *parent;
    char name[100]; const unsigned char *data;
    char path[4096];
    size_t extent, count, block_size; uint64_t size, clock, reads, decodes;
    double deadline; int opens;
    Frame *frames; int *map; Slot slots[SLOTS];
    unsigned char *cache; ZSTD_DCtx *decoder;
};
static int expired(Ctx *c) {
    struct timespec now;
    if(clock_gettime(CLOCK_MONOTONIC,&now)!=0) return 1;
    return now.tv_sec+now.tv_nsec/1e9>=c->deadline;
}
static int close_file(sqlite3_file *p) { File *f=(File*)p;int r=f->parent->pMethods->xClose(f->parent);f->ctx->opens--;return r; }
static int read_file(sqlite3_file *p, void *out, int n, sqlite3_int64 offset) {
    Ctx *c=((File*)p)->ctx; unsigned char *target=out;
    if(n<0 || n>1048576 || offset<0 || expired(c)) return SQLITE_IOERR_READ;
    c->reads++;
    uint64_t pos=(uint64_t)offset; int left=n;
    while(left>0 && pos<c->size) {
        size_t index=(size_t)(pos/c->block_size), start=(size_t)(pos%c->block_size);
        if(index>=c->count) return SQLITE_IOERR_READ;
        int slot=c->map[index];
        if(slot<0) {
            unsigned selected=0;
            for(unsigned i=1;i<SLOTS;i++) if(c->slots[i].age<c->slots[selected].age) selected=i;
            slot=(int)selected;
            if(c->slots[slot].block!=UINT64_MAX) c->map[c->slots[slot].block]=-1;
            c->slots[slot].block=UINT64_MAX;c->slots[slot].age=0;
            Frame f=c->frames[index];
            size_t decoded=ZSTD_decompressDCtx(c->decoder,c->cache+(size_t)slot*c->block_size,c->block_size,c->data+f.off,f.len);
            size_t expected=(size_t)((c->size-(uint64_t)index*c->block_size)<c->block_size?c->size-(uint64_t)index*c->block_size:c->block_size);
            if(ZSTD_isError(decoded) || (decoded!=expected && decoded!=c->block_size)) return SQLITE_IOERR_READ;
            c->slots[slot].block=index; c->map[index]=slot; c->decodes++;
        }
        c->slots[slot].age=++c->clock;
        size_t take=c->block_size-start;
        if(take>(size_t)left) take=(size_t)left;
        if(take>c->size-pos) take=(size_t)(c->size-pos);
        memcpy(target,c->cache+(size_t)slot*c->block_size+start,take);
        target+=take; pos+=take; left-=(int)take;
    }
    if(left) { memset(target,0,(size_t)left); return SQLITE_IOERR_SHORT_READ; }
    return SQLITE_OK;
}
static int readonly_write(sqlite3_file *p,const void *b,int n,sqlite3_int64 o) { (void)p;(void)b;(void)n;(void)o;return SQLITE_READONLY; }
static int readonly_truncate(sqlite3_file *p,sqlite3_int64 n) { (void)p;(void)n;return SQLITE_READONLY; }
static int sync_file(sqlite3_file *p,int flags) { (void)p;(void)flags;return SQLITE_OK; }
static int file_size(sqlite3_file *p,sqlite3_int64 *size) { *size=(sqlite3_int64)((File*)p)->ctx->size;return SQLITE_OK; }
static int lock_file(sqlite3_file *p,int level) { File *f=(File*)p;return f->parent->pMethods->xLock(f->parent,level); }
static int unlock_file(sqlite3_file *p,int level) { File *f=(File*)p;return f->parent->pMethods->xUnlock(f->parent,level); }
static int reserved(sqlite3_file *p,int *v) { File *f=(File*)p;return f->parent->pMethods->xCheckReservedLock(f->parent,v); }
static int shm_map(sqlite3_file *p,int page,int size,int extend,void volatile **out) { File *f=(File*)p;return f->parent->pMethods->xShmMap(f->parent,page,size,extend,out); }
static int shm_lock(sqlite3_file *p,int offset,int n,int flags) { File *f=(File*)p;return f->parent->pMethods->xShmLock(f->parent,offset,n,flags); }
static void shm_barrier(sqlite3_file *p) { File *f=(File*)p;f->parent->pMethods->xShmBarrier(f->parent); }
static int shm_unmap(sqlite3_file *p,int remove) { File *f=(File*)p;return f->parent->pMethods->xShmUnmap(f->parent,remove); }
static int control(sqlite3_file *p,int op,void *v) { (void)p;(void)op;(void)v;return SQLITE_NOTFOUND; }
static int sector(sqlite3_file *p) { (void)p;return 4096; }
static int characteristics(sqlite3_file *p) { (void)p;return 0; }
/* Keep engine RW metadata like the Python VFS: SQLite 3.53.4 ignores
   damaged CHECK constraints on RO handles. Query-only is set by the caller;
   source xWrite/xTruncate always refuse writes. Only private auxiliary files
   use the parent VFS, preserving closed WAL-header validation. */
static const sqlite3_io_methods methods={
    .iVersion=2,.xClose=close_file,.xRead=read_file,.xWrite=readonly_write,
    .xTruncate=readonly_truncate,.xSync=sync_file,.xFileSize=file_size,
    .xLock=lock_file,.xUnlock=unlock_file,.xCheckReservedLock=reserved,
    .xFileControl=control,.xSectorSize=sector,.xDeviceCharacteristics=characteristics,
    .xShmMap=shm_map,.xShmLock=shm_lock,.xShmBarrier=shm_barrier,.xShmUnmap=shm_unmap
};
static int auxiliary(Ctx *c,const char *name) {
    size_t n=strlen(c->path);
    return n && name && !strncmp(name,c->path,n) && (!strcmp(name+n,"-wal") || !strcmp(name+n,"-journal"));
}
static int open_file(sqlite3_vfs *v,const char *name,sqlite3_file *file,int flags,int *out) {
    Ctx *c=v->pAppData;
    if(!(flags&SQLITE_OPEN_MAIN_DB)) return auxiliary(c,name)?c->parent->xOpen(c->parent,name,file,flags,out):SQLITE_CANTOPEN;
    if(!name || strlen(name)>=sizeof(c->path) || (c->path[0] && strcmp(name,c->path))) return SQLITE_CANTOPEN;
    strcpy(c->path,name);
    File *f=(File*)file;f->parent=(sqlite3_file*)(f+1);
    int r=c->parent->xOpen(c->parent,name,f->parent,flags,out);
    if(r!=SQLITE_OK)return r;
    if(f->parent->pMethods->iVersion<2) { f->parent->pMethods->xClose(f->parent);return SQLITE_CANTOPEN; }
    f->base.pMethods=&methods;f->ctx=c;f->ctx->opens++;
    if(out) *out=flags;return SQLITE_OK;
}
static int delete_file(sqlite3_vfs *v,const char *p,int sync) { Ctx *c=v->pAppData;return auxiliary(c,p)?c->parent->xDelete(c->parent,p,sync):SQLITE_READONLY; }
static int access_file(sqlite3_vfs *v,const char *p,int flags,int *out) { Ctx *c=v->pAppData;return c->parent->xAccess(c->parent,p,flags,out); }
static int pathname(sqlite3_vfs *v,const char *p,int n,char *out) {
    Ctx *c=v->pAppData;return c->parent->xFullPathname(c->parent,p,n,out);
}
static int randomness(sqlite3_vfs *v,int n,char *out) { Ctx *c=v->pAppData;return c->parent->xRandomness(c->parent,n,out); }
static int sleep_vfs(sqlite3_vfs *v,int n) { Ctx *c=v->pAppData;return c->parent->xSleep(c->parent,n); }
static int current_time(sqlite3_vfs *v,double *out) { Ctx *c=v->pAppData;return c->parent->xCurrentTime(c->parent,out); }
static void release(Ctx *c) {
    if(!c)return;ZSTD_freeDCtx(c->decoder);free(c->frames);free(c->map);free(c->cache);free(c);
}
/* Public offsets still describe64KiB records. Multipart records contain
   independent page frames, so an indexed4KiB read need not decode64KiB.
   Old single-frame records retain their exact decoder and size semantics. */
void *cwcvfs_create(const char *name,const void *data,size_t extent,const uint64_t *offsets,const uint64_t *lengths,size_t count,uint64_t size,double deadline) {
    if(!sqlite3_api || !name || strlen(name)>=100 || !data || (count && (!offsets || !lengths)) || !isfinite(deadline) || size>INT64_MAX || count!=(size/BLOCK+(size%BLOCK!=0)) || count>SIZE_MAX/sizeof(Frame) || count>SIZE_MAX/sizeof(int) || sqlite3_vfs_find(name)) return NULL;
    const unsigned char *bytes=data;
    size_t page=BLOCK;
    if(count) {
        if(offsets[0]>extent || !lengths[0] || lengths[0]>extent-offsets[0]) return NULL;
        size_t first=ZSTD_findFrameCompressedSize(bytes+offsets[0],lengths[0]);
        if(!ZSTD_isError(first) && first<lengths[0]) {
            unsigned long long raw=ZSTD_getFrameContentSize(bytes+offsets[0],first);
            if(raw!=4096 && raw!=8192 && raw!=16384) return NULL;
            page=(size_t)raw;
        }
    }
    uint64_t entries=size/page+(size%page!=0);
    if(entries>SIZE_MAX/sizeof(Frame) || entries>SIZE_MAX/sizeof(int)) return NULL;
    Ctx *c=calloc(1,sizeof(*c));if(!c)return NULL;
    c->data=data;c->extent=extent;c->size=size;c->count=(size_t)entries;c->block_size=page;c->deadline=deadline;
    c->frames=malloc((entries?entries:1)*sizeof(Frame));c->map=malloc((entries?entries:1)*sizeof(int));
    c->cache=malloc(page*SLOTS);c->decoder=ZSTD_createDCtx();
    if(!c->frames || !c->map || !c->cache || !c->decoder) { release(c);return NULL; }
    size_t output=0;uint64_t raw_position=0;
    for(size_t i=0;i<count;i++) {
        if(offsets[i]>extent || !lengths[i] || lengths[i]>extent-offsets[i] || (!(i%1024) && expired(c))) { release(c);return NULL; }
        uint64_t need=size-raw_position;if(need>BLOCK)need=BLOCK;
        raw_position+=need;
        if(page==BLOCK) {
            c->frames[output]=(Frame){offsets[i],lengths[i]};c->map[output++]=-1;continue;
        }
        uint64_t offset=offsets[i],end=offset+lengths[i];
        while(offset<end) {
            size_t length=ZSTD_findFrameCompressedSize(bytes+offset,(size_t)(end-offset));
            if(ZSTD_isError(length) || !length || output>=c->count || !need) { release(c);return NULL; }
            unsigned long long decoded=ZSTD_getFrameContentSize(bytes+offset,length);
            uint64_t expected=need<page?need:page;
            if(decoded!=expected) { release(c);return NULL; }
            c->frames[output]=(Frame){offset,length};c->map[output++]=-1;
            offset+=length;need-=expected;
        }
        if(need) { release(c);return NULL; }
    }
    if(output!=c->count) { release(c);return NULL; }
    for(unsigned i=0;i<SLOTS;i++) c->slots[i].block=UINT64_MAX;
    strcpy(c->name,name);c->parent=sqlite3_vfs_find(NULL);
    if(!c->parent) { release(c);return NULL; }
    c->vfs=(sqlite3_vfs){.iVersion=1,.szOsFile=(int)sizeof(File)+c->parent->szOsFile,.mxPathname=c->parent->mxPathname,.zName=c->name,.pAppData=c,
        .xOpen=open_file,.xDelete=delete_file,.xAccess=access_file,.xFullPathname=pathname,
        .xRandomness=randomness,.xSleep=sleep_vfs,.xCurrentTime=current_time};
    if(sqlite3_vfs_register(&c->vfs,0)!=SQLITE_OK) { release(c);return NULL; }
    return c;
}
int cwcvfs_destroy(void *p) { Ctx *c=p;if(!c)return SQLITE_MISUSE;if(c->opens)return SQLITE_BUSY;int r=sqlite3_vfs_unregister(&c->vfs);if(r==SQLITE_OK)release(c);return r; }
void cwcvfs_stats(void *p,uint64_t *reads,uint64_t *decodes) { Ctx *c=p;*reads=c->reads;*decodes=c->decodes; }
int sqlite3_cwcvfs_init(sqlite3 *db,char **error,const sqlite3_api_routines *api) {
    (void)db;(void)error;SQLITE_EXTENSION_INIT2(api);
    return sqlite3_libversion_number()==3053004 && ZSTD_versionNumber()==10507?SQLITE_OK:SQLITE_MISMATCH;
}
