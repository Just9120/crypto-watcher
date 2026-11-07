"""One bounded two-market replay. External transports are isolated test doubles."""
import asyncio
import hashlib
import hmac
import json
import os
try:
    import resource
except ImportError:
    resource=None
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from cryptowatcher.agent_delivery import AgentConfig
from cryptowatcher.bot import Controller
from cryptowatcher.config import TIMEFRAMES, Infrastructure, OwnerSettings
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market_storage import enable
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store
from cryptowatcher.universe import classify

MIN = 60000
BASE = 1_800_000_000_000
SECRET = "fixture_no_credentials_" * 3
URL = "https://agent.example/integrations/cryptowatcher/events"
WORK = Path(os.environ.get("CW_BURST_OUTPUT","/work"))
PATH = WORK / "case.sqlite3"

def proof(state, **data):
    out = WORK / "result.tmp"
    out.write_text(json.dumps({"state": state, **data}), encoding="utf-8")
    out.replace(WORK / "result.json")

def candle(end, tf, shock=False):
    minutes = int(tf[:-1])
    step = minutes * MIN
    o, c = (100, 106) if shock else (100, 100)
    return Candle.parse([end-step,o,max(o,c)+1,min(o,c)-1,c,10,20000 if shock else 400],
                        tf,"websocket",end,end)

def metadata(symbol, market):
    value = dict(symbol=symbol,baseCoin=symbol[:-4],quoteCoin="USDT",status="Trading",
                 symbolType="",priceFilter={"tickSize":"0.01"})
    if market == "linear":
        value.update(contractType="LinearPerpetual",settleCoin="USDT",isPreListing=False)
    return value

async def case():
    import burst_timing
    if os.environ.get("CW_BURST_PROFILE","1")=="1":
        burst_timing.install()
    infra=Infrastructure.from_env(dict(
        OWNER_USER_ID="100001", EXPECTED_BOT_ID="100002",
        TELEGRAM_BOT_TOKEN="100002:fixture_no_credentials_123456789",
        DATABASE_FILE=str(PATH), STATE_FILE=str(WORK/"unused.json")))
    store=Store(PATH,infra.owner_id)
    store.initialize(OwnerSettings())
    enable(store.db)
    store.bounded=True
    wall=[None]
    clock=[BASE-5*MIN]
    def now():
        return clock[0] if wall[0] is None else BASE+int((time.monotonic()-wall[0])*1000)
    store.clock_ms=now
    lags=[]
    due_lags=[]
    live_agent=[]
    all_agent=0
    accepted=0
    offered_so_far=0
    seed_streams=0
    phase="seed"
    last_checkpoint=0.0
    periodic_wall={}
    def checkpoint(force=False):
        nonlocal last_checkpoint
        tick=time.monotonic()
        if not force and tick-last_checkpoint<5:
            return
        last_checkpoint=tick
        ordered=sorted(lags)
        data=dict(phase=phase, seed_streams=seed_streams, offered_so_far=offered_so_far,
                  accepted=accepted, cpu_seconds=time.process_time(),
                  elapsed_since_seed_seconds=tick-seed_started,
                  max_source_lag_ms=max(ordered,default=None),
                  source_p95_ms=ordered[max(0,int(len(ordered)*.95)-1)] if ordered else None,
                  complete_due=len(due_lags),max_completed_due_lag_ms=max(due_lags,default=None),
                  telegram_acks=bot.send_message.await_count,agent_acks=all_agent,agent_live=len(live_agent),
                  ingestion_work=market._work_timing,periodic_elapsed_by_job=periodic_wall)
        temp=WORK/"progress.tmp"
        temp.write_text(json.dumps(data),encoding="utf-8")
        temp.replace(WORK/"progress.json")
    def receive(req):
        nonlocal all_agent
        assert str(req.url)==URL
        assert hmac.compare_digest(req.headers["x-cryptowatcher-signature"],hmac.new(SECRET.encode(),req.content,hashlib.sha256).hexdigest())
        data=json.loads(req.content)
        assert not data["trading_authorized"] and not data["model_invoked"]
        all_agent+=1
        if data["delivery_kind"] in ("LIVE_EVENT","EPISODE_UPDATE"):
            live_agent.append(data["event_id"])
        return httpx.Response(200,json=dict(event_id=data["event_id"],revision=data["revision"],receipt_id="a"*64,trading_authorized=False,model_invoked=False))
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        market=BybitRadar(store,client)
        market.server_now=now
        c=Controller(infra,store,market)
        c.agent.config=AgentConfig.from_env(dict(AGENT_EVENT_URL=URL,AGENT_PRODUCER_REF="pool-replay",AGENT_HMAC_SECRET=SECRET))
        c.agent.enable(True)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
        ctx=SimpleNamespace(bot=bot)
        work=c.outcomes.apply
        def apply(job,event,binding,result,at):
            work(job,event,binding,result,at)
            if result["state"]=="COMPLETE":
                due_lags.append(max(0,at-job["due_ms"]))
        c.outcomes.apply=apply
        streams=[]
        seed_started=time.monotonic()
        checkpoint(True)
        for kind in ("spot","linear"):
            for i in range(206):
                symbol=f"SIM{i:03d}USDT"
                inst,reason=classify(kind,metadata(symbol,kind))
                assert inst is not None,reason
                market.instruments[inst.key]=inst
                market.tickers[inst.key]={"turnover24h":"2000000","lastPrice":"100"}
                for tf in TIMEFRAMES:
                    stream=f"{inst.key}/{tf}"
                    streams.append((kind,symbol,tf,stream))
                    market.streams.add(stream)
                    step=int(tf[:-1])*MIN
                    market.sources.put(stream,[candle(BASE-j*step,tf) for j in range(24,0,-1)],recovery=True)
                    seed_streams+=1
                    checkpoint()
        # Real 5-minute obligations due at the first burst.
        for kind in ("spot","linear"):
            stream=f"{kind}/SIM000USDT/1m"
            warm=[candle(BASE-j*MIN,"1m",shock=j==5) for j in range(23,4,-1)]
            c.engine.observe(stream,warm,2e6,0.01,clock[0],live=True)
        c.episode_pool.advance(clock[0])
        seed_wall=time.monotonic()-seed_started
        wall[0]=time.monotonic()
        phase="replay"
        checkpoint(True)
        async def periodically(callback, interval):
            tick=0.0
            while True:
                await asyncio.sleep(max(0,wall[0]+tick-time.monotonic()))
                started=time.monotonic()
                try:
                    await callback(ctx)
                finally:
                    name=callback.__name__
                    metric=periodic_wall.setdefault(name,{"calls":0,"wall_seconds":0})
                    metric["calls"]+=1
                    metric["wall_seconds"]+=time.monotonic()-started
                    checkpoint()
                tick+=interval
                if tick < time.monotonic()-wall[0]-interval:
                    tick=time.monotonic()-wall[0]+interval
        jobs=[asyncio.create_task(periodically(cb,seconds)) for cb,seconds in
              ((c.deliver,1),(c.agent_job,2),(c.analytics_job,5),(c.repack_job,5))]
        try:
            for offset in (0,):
                await asyncio.sleep(max(0,wall[0]+offset-time.monotonic()))
                end=BASE+offset*1000
                phase=f"burst_{offset}"
                checkpoint(True)
                for j,(kind,symbol,tf,stream) in enumerate(streams):
                    if offset % (int(tf[:-1])*60):
                        continue
                    v=candle(end,tf,shock=(offset in (0,120)))
                    message=dict(topic=f"kline.{tf[:-1]}.{symbol}",ts=end,data=[
                        dict(interval=tf[:-1],start=v.start,end=v.end-1,open=v.open,
                             high=v.high,low=v.low,close=v.close,volume=v.volume,
                             turnover=v.turnover,confirm=True)])
                    offered_so_far+=1
                    market.accept_message(kind,message,market.streams,received_ms=now())
                    if market.sources.windows[stream].get(v.start):
                        accepted+=1
                        lags.append(max(0,now()-end))
                    checkpoint()
                    if j%4==0:
                        await asyncio.sleep(0)
            burst_completed_seconds=time.monotonic()-wall[0]
            phase="drain"
            checkpoint(True)
            await asyncio.sleep(2)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
        phase="report"
        checkpoint(True)
        now_ms=now()
        pending=[v for _,v in c.history.items("outcome-job:") if v["state"]!="COMPLETE"]
        due=[v for v in pending if v["due_ms"]<=now_ms]
        files={s:Path(str(PATH)+s).stat().st_size for s in ("","-wal","-shm") if Path(str(PATH)+s).exists()}
        lags.sort()
        result=dict(
            offered=1648,accepted=accepted,burst_completed_seconds=burst_completed_seconds,stages=dict(burst_timing.records),streams=len(streams),seed_seconds=seed_wall,
            ingress_window_seconds=time.monotonic()-wall[0],
            max_source_lag_ms=max(lags,default=None),source_p95_ms=lags[int(len(lags)*.95)-1] if lags else None,
            late_source_streams=market.confirmed_source_diagnostic(now_ms)["late_streams"],
            complete_due=len(due_lags),max_due_lag_ms=max(due_lags+[now_ms-j["due_ms"] for j in due],default=None),
            due_remaining=len(due),future_outcomes=len(pending)-len(due),
            events=c.history.count("event-head:"),telegram_sent=bot.send_message.await_count,
            telegram_pending=c.history.count("pending:telegram:"),agent_live=len(live_agent),agent_total=all_agent,
            agent_pending=c.history.count("agent:intent-due:")+c.history.count("agent:send-due:"),
            pool=dict(store.db.execute("SELECT state,count(*) FROM episode_pool GROUP BY state")),
            process_cpu_seconds=time.process_time(),peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024 if resource else None,
            working_files=files,
        )
        proof("COMPLETE",**result)
        store.close()

if __name__ == "__main__":
    try:
        asyncio.run(case())
    except BaseException as exc:
        proof("ERROR",error=type(exc).__name__)
        raise
    if resource: time.sleep(8)
