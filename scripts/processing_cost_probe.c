#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include <sys/types.h>

typedef struct sqlite3 sqlite3;
typedef struct sqlite3_stmt sqlite3_stmt;
enum { READ,WRITE,COMMIT,OTHER,PREPARE,RESET,FINALIZE,EXEC,SYNC,PREAD,PWRITE,CALLBACK,CALLBACK_JSON,CALLBACK_CW,CALLBACK_OTHER,OPS };
#define SLOTS 64
/* Calls, inclusive wall/CPU, exclusive wall/CPU;3 phases and2 roles. */
static _Atomic uint64_t counts[SLOTS][3][2][OPS][5];
static _Atomic int phase, slots, lost;
static _Atomic long tids[SLOTS];
static _Thread_local int slot=-1, role;
typedef struct scope { struct scope *parent; uint64_t wall,cpu,child_wall,child_cpu; int p,r,op,s; } Scope;
static _Thread_local Scope *top;
static _Thread_local Scope callbacks[128];
static _Thread_local int callback_depth;
static pthread_once_t once=PTHREAD_ONCE_INIT;
static int (*step_fn)(sqlite3_stmt*);
static int (*prepare_fn)(sqlite3*,const char*,int,sqlite3_stmt**,const char**);
static int (*prepare3_fn)(sqlite3*,const char*,int,unsigned int,sqlite3_stmt**,const char**);
static int (*reset_fn)(sqlite3_stmt*), (*finalize_fn)(sqlite3_stmt*);
static int (*exec_fn)(sqlite3*,const char*,int(*)(void*,int,char**,char**),void*,char**);
static const char *(*sql_fn)(sqlite3_stmt*);
static int (*readonly_fn)(sqlite3_stmt*);
static int (*fsync_fn)(int), (*fdatasync_fn)(int);
static ssize_t (*pread_fn)(int,void*,size_t,off_t), (*pread64_fn)(int,void*,size_t,off64_t);
static ssize_t (*pwrite_fn)(int,const void*,size_t,off_t), (*pwrite64_fn)(int,const void*,size_t,off64_t);
#define LOAD(x,n) do { void *address=dlsym(RTLD_NEXT,(n)); memcpy(&(x),&address,sizeof(x)); if (!(x)) _exit(125); } while(0)
static void initialize(void) {
 LOAD(step_fn,"sqlite3_step"); LOAD(prepare_fn,"sqlite3_prepare_v2"); LOAD(prepare3_fn,"sqlite3_prepare_v3");
 LOAD(reset_fn,"sqlite3_reset"); LOAD(finalize_fn,"sqlite3_finalize"); LOAD(exec_fn,"sqlite3_exec");
 LOAD(sql_fn,"sqlite3_sql"); LOAD(readonly_fn,"sqlite3_stmt_readonly");
 LOAD(fsync_fn,"fsync"); LOAD(fdatasync_fn,"fdatasync");
 LOAD(pread_fn,"pread"); LOAD(pread64_fn,"pread64"); LOAD(pwrite_fn,"pwrite"); LOAD(pwrite64_fn,"pwrite64");
}
static uint64_t now(clockid_t clock) { struct timespec ts; if(clock_gettime(clock,&ts)) _exit(125); return (uint64_t)ts.tv_sec*1000000000ULL+(uint64_t)ts.tv_nsec; }
static void enter(Scope *s,int op) {
 if(slot<0) { slot=atomic_fetch_add(&slots,1); if(slot>=SLOTS) atomic_fetch_add(&lost,1); else atomic_store(&tids[slot],syscall(SYS_gettid)); }
 s->parent=top; s->child_wall=s->child_cpu=0; s->p=atomic_load(&phase); s->r=role; s->op=op;s->s=slot;
 s->wall=now(CLOCK_MONOTONIC);s->cpu=now(CLOCK_THREAD_CPUTIME_ID);top=s;
}
static void leave(Scope *s) {
 int error=errno; uint64_t c=now(CLOCK_THREAD_CPUTIME_ID)-s->cpu,w=now(CLOCK_MONOTONIC)-s->wall;top=s->parent;
 if(top) { top->child_wall+=w;top->child_cpu+=c; }
 if(s->s<SLOTS) { _Atomic uint64_t *v=counts[s->s][s->p][s->r][s->op];
  atomic_fetch_add(v,1);atomic_fetch_add(v+1,w);atomic_fetch_add(v+2,c);
  atomic_fetch_add(v+3,w>s->child_wall?w-s->child_wall:0);atomic_fetch_add(v+4,c>s->child_cpu?c-s->child_cpu:0);
 }
 errno=error;
}
static int kind(sqlite3_stmt *stmt) {
 const char *s=sql_fn(stmt); if(s) { while(*s==' '||*s=='\n'||*s=='\t'||*s=='\r')s++;
  if(!strncasecmp(s,"COMMIT",6)||!strncasecmp(s,"END",3))return COMMIT;
 }
 return readonly_fn(stmt)?READ:WRITE;
}
int sqlite3_step(sqlite3_stmt *p) { pthread_once(&once,initialize); Scope s;enter(&s,kind(p));int r=step_fn(p);leave(&s);return r; }
int sqlite3_prepare_v2(sqlite3 *d,const char *q,int n,sqlite3_stmt **s,const char **t) { pthread_once(&once,initialize); Scope c;enter(&c,PREPARE);int r=prepare_fn(d,q,n,s,t);leave(&c);return r; }
int sqlite3_prepare_v3(sqlite3 *d,const char *q,int n,unsigned int f,sqlite3_stmt **s,const char **t) { pthread_once(&once,initialize); Scope c;enter(&c,PREPARE);int r=prepare3_fn(d,q,n,f,s,t);leave(&c);return r; }
int sqlite3_reset(sqlite3_stmt *p) { pthread_once(&once,initialize); Scope s;enter(&s,RESET);int r=reset_fn(p);leave(&s);return r; }
int sqlite3_finalize(sqlite3_stmt *p) { pthread_once(&once,initialize); Scope s;enter(&s,FINALIZE);int r=finalize_fn(p);leave(&s);return r; }
int sqlite3_exec(sqlite3 *d,const char *q,int(*cb)(void*,int,char**,char**),void *a,char **e) { pthread_once(&once,initialize); Scope s;enter(&s,EXEC);int r=exec_fn(d,q,cb,a,e);leave(&s);return r; }
int fsync(int fd) { pthread_once(&once,initialize); Scope s;enter(&s,SYNC);int r=fsync_fn(fd);leave(&s);return r; }
int fdatasync(int fd) { pthread_once(&once,initialize); Scope s;enter(&s,SYNC);int r=fdatasync_fn(fd);leave(&s);return r; }
ssize_t pread(int f,void *b,size_t n,off_t o) { pthread_once(&once,initialize);Scope s;enter(&s,PREAD);ssize_t r=pread_fn(f,b,n,o);leave(&s);return r; }
ssize_t pread64(int f,void *b,size_t n,off64_t o) { pthread_once(&once,initialize);Scope s;enter(&s,PREAD);ssize_t r=pread64_fn(f,b,n,o);leave(&s);return r; }
ssize_t pwrite(int f,const void *b,size_t n,off_t o) { pthread_once(&once,initialize);Scope s;enter(&s,PWRITE);ssize_t r=pwrite_fn(f,b,n,o);leave(&s);return r; }
ssize_t pwrite64(int f,const void *b,size_t n,off64_t o) { pthread_once(&once,initialize);Scope s;enter(&s,PWRITE);ssize_t r=pwrite64_fn(f,b,n,o);leave(&s);return r; }
int cw_probe_phase(int p) { if(p<0||p>2)return -1;return atomic_exchange(&phase,p); }
int cw_probe_role(int r) { if(r<0||r>1)return -1;int old=role;role=r;return old; }
int cw_probe_callback_enter(int kind) { if(kind<CALLBACK||kind>=OPS){atomic_fetch_add(&lost,1);return 0;} if(callback_depth>=128){atomic_fetch_add(&lost,1);return 0;} enter(&callbacks[callback_depth++],kind);return 1; }
void cw_probe_callback_leave(void) { if(callback_depth<=0){atomic_fetch_add(&lost,1);return;}leave(&callbacks[--callback_depth]); }
/* No SQL text, parameters, paths, market data or secrets are exported. */
int cw_probe_snapshot(char *buf,size_t cap) {
 size_t used=0;int first=1;
 #define ADD(...) do { int appended=snprintf(buf+used,cap>used?cap-used:0,__VA_ARGS__);if(appended<0||(size_t)appended>=cap-used)return -1;used+=(size_t)appended; }while(0)
 ADD("{\"lost\":%d,\"slots\":%d,\"rows\":[",atomic_load(&lost),atomic_load(&slots));
 for(int s=0;s<SLOTS;s++)for(int p=0;p<3;p++)for(int r=0;r<2;r++)for(int op=0;op<OPS;op++) {
  uint64_t n=atomic_load(&counts[s][p][r][op][0]);if(!n)continue;
  ADD("%s[%ld,%d,%d,%d,%llu",first?"":",",atomic_load(&tids[s]),p,r,op,(unsigned long long)n);first=0;
  for(int j=1;j<5;j++){ADD(",%llu",(unsigned long long)atomic_load(&counts[s][p][r][op][j]));} ADD("]");
 }
 ADD("]}");return (int)used;
 #undef ADD
}

uint64_t cw_probe_calibrate(void) { uint64_t began=now(CLOCK_THREAD_CPUTIME_ID); for(int i=0;i<10000;i++){Scope s;enter(&s,OTHER);leave(&s);}return now(CLOCK_THREAD_CPUTIME_ID)-began; }
