"""Independent durable advisory outbox; secrets and I/O stay outside state transactions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .agent_contract import envelope
from .events import PREFIX, History, identity
from .storage import StateError, Store, encode


@dataclass(frozen=True)
class AgentConfig:
    state: str = "UNSET"
    reason: str = "INTEGRATION_NOT_CONFIGURED"
    url: str = field(default="", repr=False)
    producer_ref: str = ""
    secret: str = field(default="", repr=False)
    access_id: str = field(default="", repr=False)
    access_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        values = os.environ if env is None else env
        names = (
            "AGENT_EVENT_URL",
            "AGENT_PRODUCER_REF",
            "AGENT_HMAC_SECRET",
            "AGENT_ACCESS_CLIENT_ID",
            "AGENT_ACCESS_CLIENT_SECRET",
        )
        url, ref, secret, access_id, access_secret = (values.get(k, "") for k in names)
        if not any((url, ref, secret, access_id, access_secret)):
            return cls()
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and "@" not in parsed.netloc
                and parsed.path == "/integrations/cryptowatcher/events"
                and parsed.port in (None, 443)
                and not any(c.isspace() for c in url)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", ref)
                and len(secret.encode("utf-8")) >= 32
                and bool(access_id) == bool(access_secret)
                and all("\r" not in x and "\n" not in x for x in (secret, access_id, access_secret))
            )
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            return cls("BLOCKED", "INVALID_OR_PARTIAL_INTEGRATION_CONFIG")
        return cls("READY", "QUALIFIED_TRANSPORT_CONFIG", url, ref, secret, access_id, access_secret)


class AgentDelivery:
    def __init__(
        self, store: Store, client: httpx.AsyncClient | None, config: AgentConfig | None = None
    ) -> None:
        self.store = store
        self.history = History(store)
        self.client = client
        self.config = config or AgentConfig.from_env()
        self.lock = asyncio.Lock()

    def enabled(self) -> bool:
        preference = self.history.get("agent:preference", {"schema": 1, "enabled": False})
        return (
            isinstance(preference, dict)
            and preference.get("schema") == 1
            and preference.get("enabled") is True
        )

    def enable(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("Invalid agent preference")
        previous = self.history.get("agent:preference")
        if previous is not None and (
            not isinstance(previous, dict)
            or previous.get("schema") != 1
            or type(previous.get("enabled")) is not bool
        ):
            raise StateError("Unknown agent preference preserved")
        with self.store.db:
            self.history.put("agent:preference", {"schema": 1, "enabled": value})
            if value:
                self.history.put("agent:target", {"state": "READY", "reason": "OWNER_RESUME"})

    def _intent(
        self,
        episode: dict[str, Any],
        event: dict[str, Any],
        now: int,
        outcomes: list[dict[str, Any]] | None = None,
        replay: bool = False,
    ) -> None:
        historical = (
            not event["live"]
            or episode["state"] != "ACTIVE"
            or bool(outcomes)
            or not any(c["event_id"] == event["event_id"] for c in episode["confirmations"].values())
        )
        key = "agent:intent:" + (
            event["event_id"] + ":" + str(outcomes[0]["outcome_id"]) + ":" + str(outcomes[0]["revision"])
            if outcomes
            else episode["episode_id"]
        )
        previous = self.history.get(key)
        due = previous["due_ms"] if previous else now + (0 if historical or replay else 2000)
        value = {
            "event_id": event["event_id"],
            "event_revision": event["revision"],
            "episode": episode,
            "due_ms": due,
            "outcomes": outcomes or [],
            "historical": historical,
            "replay": replay,
        }
        self.history.put(key, value)
        self.history.put(f"agent:intent-due:{due:016d}:{identity(key)}", {"key": key})

    def admit(self, event: dict[str, Any]) -> None:
        if self.enabled() and event["live"]:
            episode = self.history.get("episode-head:" + event["episode_id"])
            self._intent(episode, event, event["available_at_ms"])

    def episode_closed(self, episode: dict[str, Any]) -> None:
        if self.enabled():
            confirmation = max(episode["confirmations"].values(), key=lambda x: x["market_observation_ms"])
            event = self.history.get(f"event:{confirmation['event_id']}:{confirmation['revision']:010d}")
            self._intent(episode, event, episode["available_at_ms"])

    def outcome(self, result: dict[str, Any]) -> None:
        if self.enabled():
            event = self.history.get(f"event:{result['event_id']}:{result['event_revision']:010d}")
            episode = self.history.get("episode-head:" + event["episode_id"])
            self._intent(episode, event, result["available_at_ms"], [result])

    def replay(self, event_id: str, now: int) -> None:
        event = self.history.get("event-head:" + event_id)
        if event is None:
            raise ValueError("Unknown event identity")
        start = PREFIX + "outcome-head:"
        outcomes = [
            self.history.resolve(row[0][len(PREFIX) :], json.loads(row[1]))
            for row in self.store.db.execute(
                "SELECT key,payload_json FROM runtime WHERE key>=? AND key<? "
                "AND json_extract(payload_json,'$.event_id')=? ORDER BY key LIMIT 32",
                (start, start + "~", event_id),
            )
        ]
        with self.store.db:
            self._intent(
                self.history.get("episode-head:" + event["episode_id"]),
                event,
                now,
                outcomes[:32],
                replay=True,
            )

    @staticmethod
    def _notice(episode: dict[str, Any]) -> dict[str, Any]:
        confirmations = list(episode["confirmations"].values())
        return {
            "families": sorted({c["detector"] for c in confirmations}),
            "timeframes": sorted({c["timeframe"] for c in confirmations}),
            "strength": episode["strength"],
            "state": episode["state"],
        }

    def _freeze(self, now: int) -> None:
        for due_key, binding in self.history.items("agent:intent-due:", 1):
            key = binding["key"]
            intent = self.history.get(key)
            if intent is None:
                with self.store.db:
                    self.history.delete(due_key)
                continue
            if intent["due_ms"] > now:
                return
            event = self.history.get(f"event:{intent['event_id']}:{intent['event_revision']:010d}")
            episode = intent["episode"]
            latest = max(
                event["detection_ms"],
                event.get("receipt_ms") or 0,
                episode["available_at_ms"],
                *(x["available_at_ms"] for x in intent["outcomes"]),
            )
            if latest > now:
                self._diagnostic("WAIT_CLOCK", now)
                return
            historical = intent["historical"] or now > event["fresh_until_ms"]
            notice = self._notice(episode)
            previous = self.history.get("agent:successful:" + episode["episode_id"])
            meaningful = (
                previous is None
                or notice["state"] != previous["state"]
                or set(notice["families"]) - set(previous["families"])
                or set(notice["timeframes"]) - set(previous["timeframes"])
                or notice["strength"] >= 1.5 * previous["strength"]
            )
            if not historical and event["settings_revision"] != self.store.snapshot().revision:
                historical = True
            with self.store.db:
                if historical or meaningful or intent["replay"]:
                    sequence_key = "agent:sequence:" + event["event_id"]
                    revision = self.history.get(sequence_key, 0) + 1
                    kind = (
                        "HISTORICAL_OUTCOME_UPDATE"
                        if historical
                        else "LIVE_EVENT"
                        if previous is None
                        else "EPISODE_UPDATE"
                    )
                    payload = envelope(
                        self.history,
                        event,
                        episode,
                        producer_ref=self.config.producer_ref,
                        revision=revision,
                        kind=kind,
                        now_ms=now,
                        outcomes=intent["outcomes"],
                    )
                    body = encode(payload)
                    delivery_id = identity(f"agent/{event['event_id']}/{revision}")
                    state = "PENDING" if len(body.encode("utf-8")) <= 65536 else "BLOCKED"
                    record = {
                        "delivery_id": delivery_id,
                        "event_id": event["event_id"],
                        "episode_id": episode["episode_id"],
                        "revision": revision,
                        "kind": kind,
                        "body": body,
                        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "expires_ms": event["fresh_until_ms"],
                        "settings_revision": event["settings_revision"],
                        "state": state,
                        "reason": "PAYLOAD_LIMIT" if state == "BLOCKED" else None,
                        "attempts": 0,
                        "notice": notice,
                        "intent": intent,
                        "next_ms": now,
                    }
                    self.history.put(sequence_key, revision)
                    self.history.put("agent:outbox:" + delivery_id, record)
                    if state == "PENDING":
                        self.history.put(
                            f"agent:send-due:{now:016d}:{delivery_id}", {"delivery_id": delivery_id}
                        )
                self.history.delete(key)
                self.history.delete(due_key)

    def _diagnostic(self, state: str, now: int) -> None:
        with self.store.db:
            self.history.put("agent:target", {"state": state, "at_ms": now})

    def retry(self, now: int) -> int:
        count = 0
        with self.store.db:
            for _, record in self.history.items("agent:outbox:"):
                if record["state"] == "BLOCKED" and record["reason"] != "PAYLOAD_LIMIT":
                    record.update(state="PENDING", reason=None, next_ms=now)
                    self.history.put("agent:outbox:" + record["delivery_id"], record)
                    self.history.put(
                        f"agent:send-due:{now:016d}:{record['delivery_id']}",
                        {"delivery_id": record["delivery_id"]},
                    )
                    count += 1
            self.history.put("agent:target", {"state": "READY", "at_ms": now, "reason": "OWNER_RETRY"})
        return count

    async def work(self, now: int) -> None:
        if not self.enabled() or self.config.state != "READY" or self.lock.locked() or self.client is None:
            return
        if self.history.get("agent:target", {}).get("state") == "BLOCKED":
            return
        async with self.lock:
            self._freeze(now)
            pending = self.history.items("agent:send-due:", 1)
            if not pending:
                return
            due_key, binding = pending[0]
            record = self.history.get("agent:outbox:" + binding["delivery_id"])
            if record["next_ms"] > now:
                return
            if record["kind"] != "HISTORICAL_OUTCOME_UPDATE" and (
                now > record["expires_ms"] or record["settings_revision"] != self.store.snapshot().revision
            ):
                with self.store.db:
                    record.update(state="SUPERSEDED", reason="EXPIRED_OR_SETTINGS_CHANGED")
                    self.history.put("agent:outbox:" + record["delivery_id"], record)
                    self.history.delete(due_key)
                    intent = record["intent"]
                    event = self.history.get(f"event:{intent['event_id']}:{intent['event_revision']:010d}")
                    self._intent(intent["episode"], event, now, intent["outcomes"], replay=True)
                return
            body = record["body"].encode("utf-8")
            if hashlib.sha256(body).hexdigest() != record["body_sha256"]:
                raise StateError("Immutable agent body digest mismatch")
            headers = {
                "Content-Type": "application/json",
                "x-cryptowatcher-signature": hmac.new(
                    self.config.secret.encode("utf-8"), body, hashlib.sha256
                ).hexdigest(),
            }
            if self.config.access_id:
                headers.update(
                    {
                        "CF-Access-Client-Id": self.config.access_id,
                        "CF-Access-Client-Secret": self.config.access_secret,
                    }
                )
            with self.store.db:
                if record["state"] == "IN_FLIGHT":
                    self.history.put(
                        f"agent:attempt:{record['delivery_id']}:{record['attempts']:010d}",
                        {
                            "at_ms": now,
                            "attempt": record["attempts"],
                            "state": "AMBIGUOUS_ACK",
                            "reason": "INTERRUPTED_IN_FLIGHT",
                            "body_sha256": record["body_sha256"],
                        },
                    )
                record.update(state="IN_FLIGHT", attempts=record["attempts"] + 1)
                self.history.put("agent:outbox:" + record["delivery_id"], record)
            status, acknowledged, retry_after = None, False, 0
            try:
                # Shared client receives these secrets only on this qualified request.
                async with asyncio.timeout(12):
                    async with self.client.stream(
                        "POST",
                        self.config.url,
                        content=body,
                        headers=headers,
                        timeout=12,
                        follow_redirects=False,
                    ) as response:
                        status = response.status_code
                        raw_retry = response.headers.get("Retry-After", "")
                        if status == 429:
                            if raw_retry.isdecimal() and len(raw_retry) < 10:
                                retry_after = int(raw_retry)
                            else:
                                try:
                                    retry_at = parsedate_to_datetime(raw_retry)
                                    if retry_at.utcoffset() is not None:
                                        retry_after = max(0, int(retry_at.timestamp() - now / 1000))
                                except (ValueError, TypeError, OverflowError):
                                    pass
                        raw_body = bytearray()
                        if status == 200:
                            async for chunk in response.aiter_bytes():
                                if len(raw_body) + len(chunk) > 4096:
                                    raise ValueError("Oversized acknowledgement")
                                raw_body.extend(chunk)
                if status == 200:
                    ack = json.loads(raw_body)
                    acknowledged = (
                        isinstance(ack, dict)
                        and ack.get("event_id") == record["event_id"]
                        and type(ack.get("revision")) is int
                        and ack["revision"] == record["revision"]
                        and re.fullmatch(r"[0-9a-f]{64}", str(ack.get("receipt_id", ""))) is not None
                        and ack.get("trading_authorized") is False
                        and ack.get("model_invoked") is False
                    )
            except (httpx.HTTPError, ValueError, TimeoutError):
                pass  # Errors/response bodies may contain secrets; persist only classifications.
            with self.store.db:
                self.history.delete(due_key)
                record["state"] = (
                    "DELIVERED"
                    if acknowledged
                    else "BLOCKED"
                    if (status is not None and 300 <= status < 500 and status not in (408, 425, 429))
                    else "PENDING"
                )
                record["reason"] = (
                    None if acknowledged else "AMBIGUOUS_ACK" if status in (None, 200) else f"HTTP_{status}"
                )
                attempt = {
                    "at_ms": now,
                    "attempt": record["attempts"],
                    "state": record["state"],
                    "reason": record["reason"],
                    "body_sha256": record["body_sha256"],
                }
                self.history.put(f"agent:attempt:{record['delivery_id']}:{record['attempts']:010d}", attempt)
                if acknowledged and record["kind"] != "HISTORICAL_OUTCOME_UPDATE":
                    previous = self.history.get("agent:successful:" + record["episode_id"], {})
                    if previous.get("episode_revision", 0) <= record["intent"]["episode"]["revision"]:
                        self.history.put(
                            "agent:successful:" + record["episode_id"],
                            {**record["notice"], "episode_revision": record["intent"]["episode"]["revision"]},
                        )
                if record["state"] == "PENDING":
                    delay = max(retry_after, min(300, 2 ** min(record["attempts"] - 1, 9)))
                    record["next_ms"] = now + delay * 1000
                    self.history.put(
                        f"agent:send-due:{record['next_ms']:016d}:{record['delivery_id']}",
                        {"delivery_id": record["delivery_id"]},
                    )
                self.history.put("agent:outbox:" + record["delivery_id"], record)
                self.history.put(
                    "agent:target", {"state": record["state"], "reason": record["reason"], "at_ms": now}
                )

    def diagnostics(self) -> dict[str, Any]:
        start = PREFIX + "agent:outbox:"
        counts = dict(
            self.store.db.execute(
                "SELECT json_extract(payload_json,'$.state'),COUNT(*) FROM runtime WHERE key>=? AND key<? "
                "GROUP BY json_extract(payload_json,'$.state')",
                (start, start + "~"),
            )
        )
        preference = self.history.get("agent:preference", {"schema": 1, "enabled": False})
        preference_valid = (
            isinstance(preference, dict)
            and preference.get("schema") == 1
            and type(preference.get("enabled")) is bool
        )
        return {
            "schema": "cryptowatcher.agent_delivery.v1",
            "enabled": self.enabled(),
            "preference": "VALID" if preference_valid else "BLOCKED_UNKNOWN_PREFERENCE",
            "configuration": self.config.state,
            "configuration_reason": self.config.reason,
            "producer_ref": self.config.producer_ref or "UNSET",
            "intents": self.history.count("agent:intent:"),
            "pending": self.history.count("agent:send-due:"),
            "target": self.history.get("agent:target", {"state": "PENDING"}),
            "blocked": counts.get("BLOCKED", 0),
            "delivered": counts.get("DELIVERED", 0),
            "trading_authorized": False,
            "model_invoked": False,
        }
