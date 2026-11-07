import functools
import sqlite3
import time
from collections import defaultdict
from threading import local

records=defaultdict(lambda: {"calls":0,"cpu_inclusive":0.0,"cpu_exclusive":0.0})
state=local()

def measure(name, call, *args, **kwargs):
    stack=getattr(state,"stack",None)
    if not stack:
        if name!="observe":
            return call(*args,**kwargs)
        stack=state.stack=[]
    if name=="encode" and stack[-1:]==[]:
        return call(*args,**kwargs)
    entry=[0.0]
    started=time.thread_time(); wall=time.perf_counter()
    stack.append(entry)
    try:
        return call(*args,**kwargs)
    finally:
        elapsed=time.thread_time()-started; wall_elapsed=time.perf_counter()-wall
        stack.pop()
        metric=records[name]
        metric["calls"]+=1
        metric["wall_inclusive"]=metric.get("wall_inclusive",0)+wall_elapsed
        metric["cpu_inclusive"]+=elapsed
        metric["cpu_exclusive"]+=max(0,elapsed-entry[0])
        if stack:stack[-1][0]+=elapsed

def wrap(owner, attr, label):
    original=getattr(owner,attr)
    @functools.wraps(original)
    def measured(*args,**kwargs):
        return measure(label,original,*args,**kwargs)
    setattr(owner,attr,measured)

class Cursor(sqlite3.Cursor):
    def fetchone(self):return measure("sqlite_read",super().fetchone)
    def fetchall(self):return measure("sqlite_read",super().fetchall)
    def fetchmany(self,*args):return measure("sqlite_read",super().fetchmany,*args)

def sql_stage(sql):
    words=sql.lstrip().split(None,1)
    verb=words[0].upper() if words else ""
    if verb in {"INSERT","UPDATE","DELETE","REPLACE"}:
        return "sqlite_write"
    if verb in {"SELECT","WITH","EXPLAIN"}:
        return "sqlite_read"
    return "sqlite_control"

class Connection(sqlite3.Connection):
    def execute(self,*args):return measure(sql_stage(args[0]),self.cursor(factory=Cursor).execute,*args)
    def executemany(self,*args):return measure(sql_stage(args[0]),self.cursor(factory=Cursor).executemany,*args)

def install():
    import cryptowatcher.events as events
    import cryptowatcher.storage_lifecycle as lifecycle
    from cryptowatcher.history_codec import Codec
    connect=sqlite3.connect
    def connection(*args,**kwargs):
        kwargs.setdefault("factory",Connection)
        return connect(*args,**kwargs)
    sqlite3.connect=connection
    original_init=events.EventEngine.__init__
    @functools.wraps(original_init)
    def engine_init(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        detector=self.detector
        self.detector=lambda *a,**kw: measure("detectors",detector,*a,**kw)
    events.EventEngine.__init__=engine_init
    for owner,attr,label in (
        (events.EventEngine,"observe","observe"),

        (events.History,"_version","history_revision"),
        (events.History,"_put_raw","history_write"),
        (events.History,"get","history_read"),
        (events.History,"metadata","history_read"),
        (Codec,"pack","codec_pack"),
        (lifecycle,"track","retention_index"),
        (events,"asdict","asdict"),
        (events,"encode","encode"),
    ):
        wrap(owner,attr,label)


