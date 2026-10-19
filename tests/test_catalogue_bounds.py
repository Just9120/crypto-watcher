import bisect
import itertools
import random

from cryptowatcher import catalogue_bounds as bounds
from cryptowatcher import catalogue_reader as reader
from tests.test_catalogue_aliases import populated
from tests.test_catalogue_reader import prepare as domain_prepare


def test_fixed_spelling_ceiling_matches_exhaustive_lexicographic_order():
    pattern = ("012", "-", "ab", ":", "01")
    canonical = sorted("".join(parts) for parts in itertools.product(*pattern))
    randomizer = random.Random(19102026)
    samples = canonical + [value[:index] for value in canonical for index in range(len(value))]
    samples += [value + suffix for value in canonical for suffix in ("0", ":", "\0", "я", "🙂")]
    samples += [
        "".join(randomizer.choices("012ab:-xy\0я🙂", k=randomizer.randrange(11))) for _ in range(3000)
    ]
    for sample in samples:
        position = bisect.bisect_left(canonical, sample)
        expected = canonical[position] if position < len(canonical) else None
        assert bounds.ceiling(sample, pattern) == (expected, sample in canonical)


def test_native_keyset_ranges_match_public_order_for_canonical_and_old_cursors(store):
    domain_prepare(store, decision_without_schema=True)
    populated(store, offset=10**6, initialized=True)
    expected = list(reader.scan(store.db, reader.PREFIX))
    randomizer = random.Random(19102027)
    cursors = {
        "",
        "radar:v1:",
        "radar:v1:event-head:",
        "radar:v1:history:",
        "radar:v1:episode-event:",
        "radar:v1:decision:telegram:",
        "z",
        "я",
        "radar:v1:decision:telegram:ffffffff-ffff-ffff-ffff-ffffffffffff:9999999999~",
        "radar:v1:history:9999999999999999:ffffffff-ffff-ffff-ffff-ffffffffffff",
    }
    for key, _ in expected:
        cursors.add(key)
        cursors.add(key + ":0000000001")
        for _ in range(2):
            position = randomizer.randrange(len(key))
            cursors.add(key[:position] + randomizer.choice(("~", ":", "🙂", "A", "-")))
    cursors = sorted(cursors)
    for index, cursor in enumerate(cursors):
        for after, before in (
            (cursor, None),
            (None, cursor),
            (cursor, cursors[min(index + 5, len(cursors) - 1)]),
        ):
            selected = [
                (key, value)
                for key, value in expected
                if (after is None or key > after) and (before is None or key < before)
            ]
            for reverse in (False, True):
                wanted = list(reversed(selected)) if reverse else selected
                for limit in (None, 2):
                    assert list(
                        reader.scan(
                            store.db, reader.PREFIX, reverse=reverse, limit=limit, after=after, before=before
                        )
                    ) == (wanted if limit is None else wanted[:limit])
    # PK tuple bounds produce a search and never a computed-key temporary sort.
    namespace = reader.Namespace("event", False)
    where, parameters = bounds.keyset(
        namespace.root,
        (("public_uuid", "uuid"), ("revision", "revision")),
        namespace.root + "00000000-0000-0000-0000-000000000001:0000000001",
        None,
    )
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT public_uuid,revision FROM market_records WHERE kind=0 AND "
        + where
        + " ORDER BY public_uuid,revision LIMIT 2",
        parameters,
    ).fetchall()
    assert any("SEARCH" in row[3] for row in plan)
    assert all("TEMP B-TREE" not in row[3] for row in plan)
