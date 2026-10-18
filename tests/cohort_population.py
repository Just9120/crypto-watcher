"""A full monthly auxiliary population, without copying a production database."""

from pathlib import Path

from cryptowatcher.config import OwnerSettings
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.storage import Store


def build(path: Path) -> dict[str, int]:
    store = Store(path, 233382575)
    try:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        OutcomeWorker(store)
        db = store.db
        with db:
            values = {
                "1",
                "custom",
                "spot",
                "linear",
                "top",
                "UP",
                "DOWN",
                "NEUTRAL",
                "price",
                "turnover",
                "breakout",
                "volatility",
                "composite",
                "1m",
                "3m",
                "5m",
                "15m",
            } | {f"FIXTURE{i}USDT" for i in range(244)}
            db.executemany(
                "INSERT INTO market_cohort_names(value) VALUES(?) ON CONFLICT(value) DO NOTHING",
                ((value,) for value in sorted(values)),
            )
            names = dict(db.execute("SELECT value,id FROM market_cohort_names"))
            instruments = ",".join(
                f"({i},{names['spot' if i < 67 else 'linear']},{names[f'FIXTURE{i}USDT']})"
                for i in range(244)
            )
            frames = ",".join(f"({i},{names[value]})" for i, value in enumerate(("1m", "3m", "5m", "15m")))
            families = ",".join(
                f"({i},{names[family]},{names[direction]})"
                for i, (family, direction) in enumerate(
                    (
                        ("price", "UP"),
                        ("price", "DOWN"),
                        ("breakout", "UP"),
                        ("breakout", "DOWN"),
                        ("composite", "UP"),
                        ("composite", "DOWN"),
                        ("turnover", "NEUTRAL"),
                        ("volatility", "NEUTRAL"),
                    )
                )
            )
            db.execute(f"""WITH RECURSIVE days(d) AS(VALUES(20000) UNION ALL SELECT d+1 FROM days WHERE d<20029),
                instruments(i,market,instrument) AS(VALUES {instruments}),frames(f,timeframe) AS(VALUES {frames}),
                families(k,detector,direction) AS(VALUES {families}),horizons(h) AS(VALUES(5),(15),(30),(60))
                INSERT INTO market_cohorts SELECT 1,d,detector,{names["1"]},5,{names["custom"]},market,timeframe,instrument,{names["top"]},direction,h,1,
                    20+(i+f+k+d)%4 FROM days CROSS JOIN instruments CROSS JOIN frames CROSS JOIN families CROSS JOIN horizons""")
            total = 5036160
            strengths = 4532544
            db.execute("INSERT INTO market_cohort_revisions VALUES(1,1,?)", (total,))
            db.execute(
                """WITH RECURSIVE numbers(i) AS(VALUES(1) UNION ALL SELECT i+1 FROM numbers WHERE i<?)
                INSERT INTO market_cohort_strengths SELECT 1,printf('%016x',0x3ff0000000000000+i),
                CASE WHEN i=1 THEN ? ELSE 1 END FROM numbers""",
                (strengths, total - strengths + 1),
            )
            db.execute(
                "UPDATE market_cohort_scan SET complete=1,unknown=0,schema_version=?",
                (db.execute("PRAGMA schema_version").fetchone()[0],),
            )
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return {"groups": 936960, "events": total, "facts": 20144640, "strengths": strengths}
    finally:
        store.close()
