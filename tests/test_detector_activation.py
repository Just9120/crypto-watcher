"""Domain activation is independent from consumer notification preferences."""

from contextlib import closing

import pytest

from cryptowatcher.config import ConfigError, OwnerSettings
from cryptowatcher.detector_config import FAMILIES
from cryptowatcher.events import EventEngine
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.storage import Store

from .test_events import NOW, selected, window


def test_legacy_owner_payload_defaults_to_all_active_without_changing_alert_selection():
    payload = OwnerSettings(alert_detectors=("turnover",)).payload()
    payload.pop("active_detectors")
    migrated = OwnerSettings.from_payload(payload)
    assert migrated.active_detectors == FAMILIES
    assert migrated.alert_detectors == ("turnover",)


@pytest.mark.parametrize("selection", ["price", ["price"], ("price", "price"), ("unknown",), (False,)])
def test_invalid_domain_activation_is_rejected(selection):
    with pytest.raises(ConfigError, match="active_detectors"):
        OwnerSettings(active_detectors=selection)


def test_activation_roundtrip_and_revision_are_independent_from_alerts(tmp_path):
    path = tmp_path / "activation.db"
    with closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings())
        before = store.snapshot()
        store.update(before.revision, before.settings.changed(active_detectors=("price",)))
        saved = store.snapshot()
        assert saved.revision == before.revision + 1
        assert saved.settings.active_detectors == ("price",)
        assert saved.settings.alert_detectors == before.settings.alert_detectors
    with closing(Store(path, 233382575)) as reopened:
        assert reopened.snapshot() == saved


@pytest.mark.parametrize("market", ["spot", "linear"])
@pytest.mark.parametrize("tf", ["1m", "3m", "5m", "15m"])
@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_detector_has_no_events_or_jobs_while_notification_mute_keeps_detection(
    tmp_path, market, tf, enabled
):
    with closing(Store(tmp_path / "domain.db", 233382575)) as store:
        store.initialize(OwnerSettings(active_detectors=("price",) if enabled else (), alert_detectors=()))
        evaluated = []
        detector = selected("price")

        def tracked(family, *args):
            evaluated.append(family)
            return detector(family, *args)

        worker = OutcomeWorker(store)
        engine = EventEngine(store, tracked, on_event=worker.admit)
        engine.observe(f"{market}/BTCUSDT/{tf}", window(tf=tf), 1e6, 0.01, NOW, live=True)
        assert evaluated == (["price"] if enabled else [])
        assert len(engine.history.events()) == int(enabled)
        assert len(engine.history.items("outcome-job:")) == (4 if enabled else 0)
        assert not engine.history.items("pending:telegram:")


@pytest.mark.parametrize("bounded", [False, True])
def test_activation_change_applies_to_next_observation_without_erasing_existing_origin(store, bounded):
    from cryptowatcher.market_storage import enable

    from .test_events import observe

    if bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    evaluated = []
    detector = selected("price", "composite")

    def track(family, *args):
        evaluated.append(family)
        return detector(family, *args)

    engine = EventEngine(store, track, on_event=OutcomeWorker(store).admit)
    observe(engine)
    original = engine.history.events()
    saved = store.snapshot()
    store.update(saved.revision, saved.settings.changed(active_detectors=("composite",)))
    evaluated.clear()
    observe(engine, NOW + 60_000, market="linear")
    assert evaluated == ["composite"]
    assert all(
        e["detector_id"] == "composite" for e in engine.history.events() if e["market_type"] == "linear"
    )
    assert all(engine.history.get(f"event:{e['event_id']}:0000000001") == e for e in original)
    assert len(engine.history.items("outcome-job:")) == 8
    saved = store.snapshot()
    store.update(saved.revision, saved.settings.changed(active_detectors=()))
    before = (engine.history.events(), engine.history.items("outcome-job:"))
    evaluated.clear()
    observe(engine, NOW + 120_000, market="linear")
    assert evaluated == []
    assert (engine.history.events(), engine.history.items("outcome-job:")) == before
