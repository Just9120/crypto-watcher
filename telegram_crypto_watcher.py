#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Crypto Watcher — Sprint 1.1
- Bybit / CoinMarketCap
- Top-N / custom list
- Alerts on % move from rolling baseline
- Per-chat settings/state
- Improved inline settings UI
- Mute / reset base actions in Telegram

Sprint 1.1 changes vs Sprint 1:
  • thread-safe HTTP sessions via threading.local (no more shared Session)
  • two separate executor pools: _fetch_pool + _kline_pool (no deadlock)
  • whitelist enforced in poll_engine_job (not only in handlers)
  • status_text() no longer mutates state (baselines created only by poll)
  • fetch duration logged for diagnostics
  • as_completed import moved to top level
  • asyncio.get_event_loop() → asyncio.get_running_loop()
  • .env.example and README updated

Tested target deps:
    python-telegram-bot[job-queue]==20.8
    requests>=2.31.0
    python-dotenv>=1.0.0
"""

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# ---------------------------------------------------------------
# Logging
# ---------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ---------------------------------------------------------------
# Thread-safe HTTP sessions (one per thread)
# ---------------------------------------------------------------
_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Return a per-thread requests.Session with retry."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        _thread_local.session = s
    return _thread_local.session


# ---------------------------------------------------------------
# Executor pools (separate to avoid deadlock)
# ---------------------------------------------------------------
_fetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fetch")
_kline_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="kline")

# ---------------------------------------------------------------
# Asyncio lock for state mutations
# ---------------------------------------------------------------
_state_lock = asyncio.Lock()

# ---------------------------------------------------------------
# Core/reference caches
# ---------------------------------------------------------------
CORE_UNIVERSE_TTL_SEC = int(os.getenv("CORE_UNIVERSE_TTL_SEC", "10800") or "10800")
REFERENCE_PRICE_TTL_SEC = int(os.getenv("REFERENCE_PRICE_TTL_SEC", "900") or "900")
CORE_REFERENCE_TF_TO_SEC = {
    "1d": 86400,
    "3d": 86400 * 3,
    "7d": 86400 * 7,
    "30d": 86400 * 30,
}

_cache_lock = threading.Lock()
_core_universe_cache: Dict[int, dict] = {}
_reference_price_cache: Dict[Tuple[str, str, str], dict] = {}


# ---------------------------------------------------------------
# Helpers: parsing / formatting
# ---------------------------------------------------------------
def _parse_interval_to_sec(raw: str, default: int = 300) -> int:
    v = (raw or "").strip().lower()
    if not v:
        return default
    try:
        if v.endswith("m"):
            return int(float(v[:-1]) * 60)
        if v.endswith("s"):
            return int(float(v[:-1]))
        return int(float(v)) * 60
    except Exception:
        return default


def _fmt_interval(sec: int) -> str:
    sec = int(sec)
    return f"{sec // 60}m" if sec % 60 == 0 else f"{sec}s"


def _parse_change_tf(raw: str) -> Tuple[str, str]:
    """Returns (label e.g. '30m', Bybit interval value e.g. '30')."""
    v = (raw or "5m").strip().lower()
    if v.endswith("m"):
        raw_num = v[:-1]
    else:
        raw_num = v
        v = f"{v}m"
    try:
        minutes = str(int(float(raw_num)))
    except Exception:
        v = "5m"
        minutes = "5"
    return v, minutes


def _parse_csv(raw: str) -> List[str]:
    return [x.strip().upper() for x in (raw or "").split(",") if x.strip()]


def _normalize_bybit_pair(raw: str) -> str:
    s = raw.strip().upper().replace(" ", "")
    if not s:
        return ""
    if "/" in s:
        s = s.replace("/", "")
    if s.endswith("USDT"):
        return s
    return f"{s}USDT"


def _normalize_cmc_symbol(raw: str) -> str:
    return raw.strip().upper().replace(" ", "")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _selected(label: str, active: bool) -> str:
    return f"✅ {label}" if active else label


def _truncate_list(items: List[str], max_items: int = 8) -> str:
    if not items:
        return "—"
    if len(items) <= max_items:
        return ", ".join(items)
    return ", ".join(items[:max_items]) + f" … (+{len(items) - max_items})"


# ---------------------------------------------------------------
# Env defaults
# ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()
assert TELEGRAM_BOT_TOKEN, "Missing TELEGRAM_BOT_TOKEN in .env / environment"

DEFAULT_PRICER = os.getenv("PRICER", "CMC").strip().upper()
DEFAULT_BYBIT_CATEGORY = os.getenv("BYBIT_CATEGORY", "spot").strip().lower()
DEFAULT_WATCHLIST = _parse_csv(os.getenv("WATCHLIST", "BTC,ETH"))
DEFAULT_BYBIT_PAIRS = _parse_csv(os.getenv("BYBIT_PAIRS", ""))
if not DEFAULT_BYBIT_PAIRS:
    DEFAULT_BYBIT_PAIRS = [_normalize_bybit_pair(s) for s in DEFAULT_WATCHLIST]
DEFAULT_BYBIT_TOP_LIMIT = int(os.getenv("BYBIT_TOP_LIMIT", "0") or "0")
DEFAULT_CMC_TOP_LIMIT = int(os.getenv("CMC_TOP_LIMIT", "0") or "0")
DEFAULT_THRESHOLD_PERCENT = float(os.getenv("THRESHOLD_PERCENT", "20"))
DEFAULT_POLL_INTERVAL_SEC = _parse_interval_to_sec(os.getenv("POLL_INTERVAL", "5m"), default=300)
DEFAULT_COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "10") or "10")
DEFAULT_CONVERT = os.getenv("CONVERT", "USDT").strip().upper()
DEFAULT_SHOW_TF_CHANGE = os.getenv("SHOW_TF_CHANGE", "1") == "1"
DEFAULT_CHANGE_TF_LABEL, DEFAULT_CHANGE_TF_BYBIT = _parse_change_tf(os.getenv("CHANGE_TF", "30m"))
CHAT_ID_FALLBACK = os.getenv("CHAT_ID", "").strip()
STATE_FILE = os.getenv("STATE_FILE", "state_crypto_watcher.json").strip()
ENGINE_INTERVAL_SEC = int(os.getenv("ENGINE_INTERVAL_SEC", "60") or "60")

# ---- Authorization whitelist (optional) ----
_raw_allowed = os.getenv("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS: set = set()
if _raw_allowed:
    for _x in _raw_allowed.split(","):
        _x = _x.strip()
        if _x:
            try:
                ALLOWED_CHAT_IDS.add(int(_x))
            except ValueError:
                pass


def _is_authorized(chat_id: int) -> bool:
    """If ALLOWED_CHAT_IDS is empty — everyone is allowed (backward compat)."""
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


# ---------------------------------------------------------------
# State model
# ---------------------------------------------------------------
state: Dict[str, dict] = {"version": 2, "chats": {}}


def _default_settings() -> dict:
    return {
        "pricer": DEFAULT_PRICER,
        "threshold_percent": DEFAULT_THRESHOLD_PERCENT,
        "poll_interval_sec": DEFAULT_POLL_INTERVAL_SEC,
        "cooldown_min": DEFAULT_COOLDOWN_MIN,
        "convert": DEFAULT_CONVERT,
        "show_tf_change": DEFAULT_SHOW_TF_CHANGE,
        "change_tf_label": DEFAULT_CHANGE_TF_LABEL,
        "change_tf_bybit": DEFAULT_CHANGE_TF_BYBIT,
        "bybit_category": DEFAULT_BYBIT_CATEGORY,
        "bybit_pairs": list(DEFAULT_BYBIT_PAIRS),
        "bybit_top_limit": DEFAULT_BYBIT_TOP_LIMIT if DEFAULT_BYBIT_TOP_LIMIT > 0 else 100,
        "watchlist": list(DEFAULT_WATCHLIST),
        "cmc_top_limit": DEFAULT_CMC_TOP_LIMIT if DEFAULT_CMC_TOP_LIMIT > 0 else 100,
        "core_baseline_enabled": False,
        "core_top_n": 20,
        "core_reference_tf": "7d",
    }


def _default_chat_state() -> dict:
    return {
        "enabled": True,
        "created_at": _utc_now_iso(),
        "settings": _default_settings(),
        "baselines": {},
        "mutes": {},
        "runtime": {
            "last_poll_ts": 0,
        },
    }


def _ensure_chat_shape(chat_state: dict) -> dict:
    base = _default_chat_state()
    if not isinstance(chat_state, dict):
        return base
    base["enabled"] = bool(chat_state.get("enabled", True))
    base["created_at"] = chat_state.get("created_at", base["created_at"])
    settings = chat_state.get("settings", {})
    base_settings = _default_settings()
    if isinstance(settings, dict):
        base_settings.update(settings)
    base_settings["bybit_pairs"] = [_normalize_bybit_pair(x) for x in base_settings.get("bybit_pairs", []) if x]
    base_settings["watchlist"] = [_normalize_cmc_symbol(x) for x in base_settings.get("watchlist", []) if x]
    base_settings["pricer"] = str(base_settings.get("pricer", DEFAULT_PRICER)).upper()
    base_settings["bybit_category"] = str(base_settings.get("bybit_category", DEFAULT_BYBIT_CATEGORY)).lower()
    base_settings["threshold_percent"] = float(base_settings.get("threshold_percent", DEFAULT_THRESHOLD_PERCENT))
    base_settings["poll_interval_sec"] = int(base_settings.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC))
    base_settings["cooldown_min"] = int(base_settings.get("cooldown_min", DEFAULT_COOLDOWN_MIN))
    base_settings["bybit_top_limit"] = int(base_settings.get("bybit_top_limit", _default_settings()["bybit_top_limit"]))
    base_settings["cmc_top_limit"] = int(base_settings.get("cmc_top_limit", _default_settings()["cmc_top_limit"]))
    base_settings["convert"] = str(base_settings.get("convert", DEFAULT_CONVERT)).upper()
    base_settings["show_tf_change"] = bool(base_settings.get("show_tf_change", DEFAULT_SHOW_TF_CHANGE))
    base_settings["core_baseline_enabled"] = bool(base_settings.get("core_baseline_enabled", False))
    core_top_n = int(base_settings.get("core_top_n", 20))
    base_settings["core_top_n"] = core_top_n if core_top_n in (20, 30) else 20
    core_reference_tf = str(base_settings.get("core_reference_tf", "7d")).lower()
    base_settings["core_reference_tf"] = core_reference_tf if core_reference_tf in CORE_REFERENCE_TF_TO_SEC else "7d"
    tf_label, tf_bybit = _parse_change_tf(str(base_settings.get("change_tf_label", DEFAULT_CHANGE_TF_LABEL)))
    base_settings["change_tf_label"] = tf_label
    base_settings["change_tf_bybit"] = tf_bybit
    base["settings"] = base_settings
    if isinstance(chat_state.get("baselines"), dict):
        base["baselines"] = chat_state["baselines"]
    if isinstance(chat_state.get("mutes"), dict):
        base["mutes"] = chat_state["mutes"]
    if isinstance(chat_state.get("runtime"), dict):
        base["runtime"].update(chat_state["runtime"])
    return base


def save_state() -> None:
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def load_state() -> None:
    global state
    if not os.path.exists(STATE_FILE):
        state = {"version": 2, "chats": {}}
    else:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logging.warning(f"State load failed, using empty state: {e}")
            raw = {}
        if isinstance(raw, dict) and raw.get("version") == 2 and isinstance(raw.get("chats"), dict):
            chats = {}
            for chat_id, chat_state in raw["chats"].items():
                chats[str(chat_id)] = _ensure_chat_shape(chat_state)
            state = {"version": 2, "chats": chats}
        else:
            migrated = {"version": 2, "chats": {}}
            old_chat_id = ""
            if isinstance(raw, dict):
                old_chat_id = str(raw.get("chat_id", "") or CHAT_ID_FALLBACK).strip()
            if old_chat_id:
                c = _default_chat_state()
                if isinstance(raw, dict) and isinstance(raw.get("baselines"), dict):
                    c["baselines"] = raw["baselines"]
                migrated["chats"][old_chat_id] = c
            state = migrated
    if CHAT_ID_FALLBACK and CHAT_ID_FALLBACK not in state["chats"]:
        state["chats"][CHAT_ID_FALLBACK] = _default_chat_state()
    save_state()


def get_chat_state(chat_id: int | str) -> dict:
    key = str(chat_id)
    if key not in state["chats"]:
        state["chats"][key] = _default_chat_state()
    else:
        state["chats"][key] = _ensure_chat_shape(state["chats"][key])
    return state["chats"][key]


# ---------------------------------------------------------------
# Settings logic
# ---------------------------------------------------------------
def current_mode(settings: dict) -> str:
    if settings["pricer"] == "BYBIT":
        return "top" if int(settings.get("bybit_top_limit", 0)) > 0 else "list"
    return "top" if int(settings.get("cmc_top_limit", 0)) > 0 else "list"


def set_mode(settings: dict, mode: str) -> None:
    mode = mode.lower()
    if settings["pricer"] == "BYBIT":
        if mode == "top":
            if int(settings.get("bybit_top_limit", 0)) <= 0:
                settings["bybit_top_limit"] = 100
        else:
            settings["bybit_top_limit"] = 0
    else:
        if mode == "top":
            if int(settings.get("cmc_top_limit", 0)) <= 0:
                settings["cmc_top_limit"] = 100
        else:
            settings["cmc_top_limit"] = 0


def tracked_desc(settings: dict) -> str:
    pricer = settings["pricer"]
    mode = current_mode(settings)
    if pricer == "BYBIT":
        if mode == "top":
            return f"Top {settings['bybit_top_limit']} Bybit ({settings['bybit_category']})"
        return _truncate_list(settings["bybit_pairs"])
    else:
        if mode == "top":
            return f"Top {settings['cmc_top_limit']} CMC"
        return _truncate_list(settings["watchlist"])


def price_unit(settings: dict) -> str:
    return "USDT" if settings["pricer"] == "BYBIT" else settings["convert"]


def _mode_label(settings: dict) -> str:
    mode = current_mode(settings)
    if mode == "top":
        if settings["pricer"] == "BYBIT":
            return f"Top {settings['bybit_top_limit']}"
        return f"Top {settings['cmc_top_limit']}"
    return "List"


# ---------------------------------------------------------------
# Fetching quotes  (all sync — called via run_in_executor)
# ---------------------------------------------------------------
def fetch_quotes_cmc(settings: dict, symbols: List[str]) -> Dict[str, dict]:
    if not CMC_API_KEY:
        raise RuntimeError("CMC_API_KEY is missing in environment")
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {"symbol": ",".join(symbols), "convert": settings["convert"]}
    r = _get_session().get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    out: Dict[str, dict] = {}
    for sym, payload in data.items():
        q = payload["quote"][settings["convert"]]
        out[sym.upper()] = {
            "price": float(q["price"]),
            "percent_change_1h": float(q.get("percent_change_1h") or 0.0),
            "percent_change_24h": float(q.get("percent_change_24h") or 0.0),
            "last_updated": payload.get("last_updated"),
            "rank": None,
        }
    return out


def fetch_top_cmc(settings: dict) -> Dict[str, dict]:
    if not CMC_API_KEY:
        raise RuntimeError("CMC_API_KEY is missing in environment")
    limit = int(settings["cmc_top_limit"])
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {
        "start": 1,
        "limit": limit,
        "convert": settings["convert"],
        "sort": "market_cap",
    }
    r = _get_session().get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    out: Dict[str, dict] = {}
    for idx, payload in enumerate(data, start=1):
        sym = payload["symbol"].upper()
        q = payload["quote"][settings["convert"]]
        out[sym] = {
            "price": float(q["price"]),
            "percent_change_1h": float(q.get("percent_change_1h") or 0.0),
            "percent_change_24h": float(q.get("percent_change_24h") or 0.0),
            "last_updated": payload.get("last_updated"),
            "rank": idx,
        }
    return out


def _fetch_single_kline(base_url: str, settings: dict, sym: str) -> Tuple[str, float | None]:
    """Fetch one kline TF-change for a symbol. Returns (sym, pct_change | None)."""
    try:
        k = _get_session().get(
            f"{base_url}/v5/market/kline",
            params={
                "category": settings["bybit_category"],
                "symbol": sym,
                "interval": settings["change_tf_bybit"],
                "limit": 2,
            },
            timeout=15,
        )
        k.raise_for_status()
        kd = k.json()
        if kd.get("retCode") != 0:
            return sym, None
        kl = kd.get("result", {}).get("list") or []
        if len(kl) >= 2:
            last_close = float(kl[0][4])
            prev_close = float(kl[1][4])
            if prev_close:
                return sym, (last_close / prev_close - 1.0) * 100.0
    except Exception:
        pass
    return sym, None


def _attach_bybit_tf_change(settings: dict, out: Dict[str, dict]) -> None:
    """Attach TF-change metric. Uses dedicated kline pool for parallel requests."""
    if not settings.get("show_tf_change"):
        logging.info("TF enrichment outcome=skipped reason=disabled")
        return
    base_url = "https://api.bybit.com"
    symbols = list(out.keys())
    if not symbols:
        logging.info("TF enrichment outcome=skipped reason=no_symbols")
        return

    total = len(symbols)
    ok = 0
    failed = 0
    timeout = 0

    BATCH = 25
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i : i + BATCH]
        futures = {
            _kline_pool.submit(_fetch_single_kline, base_url, settings, sym): sym
            for sym in batch
        }
        done = set()
        try:
            for fut in as_completed(futures, timeout=60):
                done.add(fut)
                try:
                    sym, pct = fut.result()
                    if pct is not None:
                        out[sym]["percent_change_tf"] = pct
                        ok += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
        except FuturesTimeoutError:
            pending = [fut for fut in futures if fut not in done]
            timeout += len(pending)
            for fut in pending:
                fut.cancel()
            logging.warning(f"TF enrichment batch timeout: pending={len(pending)}")

    if timeout > 0:
        outcome = "timeout"
    elif failed > 0:
        outcome = "failed"
    else:
        outcome = "ok"
    logging.info(
        f"TF enrichment outcome={outcome} ok={ok} failed={failed} timeout={timeout} total={total}"
    )


def fetch_quotes_bybit(settings: dict, pairs: List[str]) -> Dict[str, dict]:
    """Fetch quotes for a list of Bybit pairs — single bulk request + filter."""
    base_url = "https://api.bybit.com"
    r = _get_session().get(
        f"{base_url}/v5/market/tickers",
        params={"category": settings["bybit_category"]},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
    items = (data.get("result") or {}).get("list") or []
    pairs_set = {p.upper() for p in pairs}
    out: Dict[str, dict] = {}
    for it in items:
        sym = it["symbol"].upper()
        if sym not in pairs_set:
            continue
        price = float(it["lastPrice"])
        pct24 = float(it.get("price24hPcnt") or 0.0) * 100.0
        out[sym] = {
            "price": price,
            "percent_change_24h": pct24,
            "high24h": float(it.get("highPrice24h") or price),
            "low24h": float(it.get("lowPrice24h") or price),
            "rank": None,
        }
    if not out:
        raise RuntimeError("Bybit returned empty result (check category and pairs)")
    _attach_bybit_tf_change(settings, out)
    return out


def fetch_bybit_top(settings: dict) -> Dict[str, dict]:
    base_url = "https://api.bybit.com"
    limit = int(settings["bybit_top_limit"])
    r = _get_session().get(
        f"{base_url}/v5/market/tickers",
        params={"category": settings["bybit_category"]},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
    items = (data.get("result") or {}).get("list") or []
    if not items:
        raise RuntimeError("Bybit returned empty ticker list")
    items_sorted = sorted(
        items,
        key=lambda it: float(it.get("turnover24h") or 0.0),
        reverse=True,
    )[:limit]
    out: Dict[str, dict] = {}
    for idx, it in enumerate(items_sorted, start=1):
        sym = it["symbol"].upper()
        price = float(it["lastPrice"])
        pct24 = float(it.get("price24hPcnt") or 0.0) * 100.0
        out[sym] = {
            "price": price,
            "percent_change_24h": pct24,
            "high24h": float(it.get("highPrice24h") or price),
            "low24h": float(it.get("lowPrice24h") or price),
            "rank": idx,
        }
    _attach_bybit_tf_change(settings, out)
    return out


def fetch_quotes_any(settings: dict) -> Dict[str, dict]:
    pricer = settings["pricer"]
    mode = current_mode(settings)
    if pricer == "BYBIT":
        if mode == "top":
            return fetch_bybit_top(settings)
        return fetch_quotes_bybit(settings, settings["bybit_pairs"])
    if mode == "top":
        return fetch_top_cmc(settings)
    return fetch_quotes_cmc(settings, settings["watchlist"])


async def _fetch_async(settings: dict) -> Dict[str, dict]:
    """Run sync fetch in thread pool so we don't block the event loop."""
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    result = await loop.run_in_executor(_fetch_pool, fetch_quotes_any, settings)
    elapsed = time.monotonic() - t0
    logging.info(f"Fetch completed: {settings['pricer']} {current_mode(settings)} — {len(result)} symbols in {elapsed:.1f}s")
    return result


def _asset_from_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s.endswith("USDT"):
        return s[:-4]
    return s


def fetch_core_universe_cmc(core_top_n: int) -> Set[str]:
    if not CMC_API_KEY:
        raise RuntimeError("CMC_API_KEY is missing in environment")
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {"start": 1, "limit": int(core_top_n), "convert": "USD", "sort": "market_cap"}
    r = _get_session().get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json().get("data") or []
    return {_normalize_cmc_symbol(item.get("symbol", "")) for item in data if item.get("symbol")}


def get_cached_core_universe(core_top_n: int) -> Optional[Set[str]]:
    now = time.time()
    with _cache_lock:
        row = _core_universe_cache.get(core_top_n)
        if row and now < float(row.get("expires_at", 0)):
            return set(row.get("symbols") or set())
    try:
        symbols = fetch_core_universe_cmc(core_top_n)
        with _cache_lock:
            _core_universe_cache[core_top_n] = {"symbols": set(symbols), "expires_at": now + CORE_UNIVERSE_TTL_SEC}
        return symbols
    except Exception as e:
        logging.warning(f"Core universe refresh failed top={core_top_n}: {e}")
        return None


def is_core_symbol(symbol: str, core_top_n: int, core_symbols: Optional[Set[str]] = None) -> bool:
    symbols = core_symbols if core_symbols is not None else get_cached_core_universe(core_top_n)
    if not symbols:
        return False
    return _asset_from_symbol(symbol) in symbols


def fetch_reference_price_cmc(symbol: str, convert: str, tf: str) -> Optional[float]:
    if not CMC_API_KEY:
        raise RuntimeError("CMC_API_KEY is missing in environment")
    tf_sec = CORE_REFERENCE_TF_TO_SEC.get(tf)
    if not tf_sec:
        return None
    end_ts = int(time.time() - tf_sec)
    start_ts = max(0, end_ts - 12 * 3600)
    url = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/historical"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {
        "symbol": _asset_from_symbol(symbol),
        "convert": convert,
        "time_start": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        "time_end": datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        "interval": "5m",
        "count": 1,
    }
    r = _get_session().get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    payload = r.json().get("data") or {}
    symbol_data = payload.get(_asset_from_symbol(symbol)) or []
    if not symbol_data:
        return None
    quotes = symbol_data[0].get("quotes") or []
    if not quotes:
        return None
    price = ((quotes[-1].get("quote") or {}).get(convert) or {}).get("price")
    return float(price) if price is not None else None


def get_reference_price(symbol: str, convert: str, tf: str) -> Optional[float]:
    key = (_asset_from_symbol(symbol), convert, tf)
    now = time.time()
    with _cache_lock:
        row = _reference_price_cache.get(key)
        if row and now < float(row.get("expires_at", 0)):
            return float(row["price"])
    try:
        price = fetch_reference_price_cmc(symbol, convert, tf)
        if price is None:
            return None
        with _cache_lock:
            _reference_price_cache[key] = {"price": float(price), "expires_at": now + REFERENCE_PRICE_TTL_SEC}
        return float(price)
    except Exception as e:
        logging.warning(f"Reference price failed symbol={symbol} tf={tf}: {e}")
        return None


async def _prepare_core_context(settings: dict, quotes: Dict[str, dict]) -> Tuple[Optional[Set[str]], Dict[str, float]]:
    if not settings.get("core_baseline_enabled"):
        return None, {}
    loop = asyncio.get_running_loop()
    core_top_n = int(settings.get("core_top_n", 20))
    core_symbols = await loop.run_in_executor(_fetch_pool, get_cached_core_universe, core_top_n)
    if not core_symbols:
        return None, {}
    tf = settings.get("core_reference_tf", "7d")
    convert = "USDT" if settings["pricer"] == "BYBIT" else settings["convert"]
    unique_assets = {_asset_from_symbol(sym) for sym in quotes.keys() if is_core_symbol(sym, core_top_n, core_symbols)}
    refs: Dict[str, float] = {}
    if not unique_assets:
        return core_symbols, refs
    for asset in unique_assets:
        ref = await loop.run_in_executor(_fetch_pool, get_reference_price, asset, convert, tf)
        if ref is not None:
            refs[asset] = ref
        else:
            logging.info(f"Core reference skipped symbol={asset} tf={tf} reason=no_reference")
    return core_symbols, refs


# ---------------------------------------------------------------
# Rendering  (pure functions — no state mutation)
# ---------------------------------------------------------------
def _symbol_key(source: str, sym: str) -> str:
    return f"{source}:{sym}"


def _rank_prefix(q: dict) -> str:
    rank = q.get("rank")
    return f"#{rank} " if rank else ""


def _sub_metrics(q: dict, settings: dict) -> str:
    parts = []
    if q.get("percent_change_24h") is not None:
        parts.append(f"24h {q['percent_change_24h']:+.2f}%")
    if q.get("percent_change_1h") is not None:
        parts.append(f"1h {q['percent_change_1h']:+.2f}%")
    if settings.get("show_tf_change") and q.get("percent_change_tf") is not None:
        parts.append(f"{settings['change_tf_label']} {q['percent_change_tf']:+.2f}%")
    return " · ".join(parts)


def _directional_marker(delta_pct: Optional[float]) -> str:
    if delta_pct is None:
        return "→"
    if abs(delta_pct) < 0.005:
        return "→"
    return "↗" if delta_pct > 0 else "↘"


def settings_text(chat_state: dict) -> str:
    s = chat_state["settings"]
    active_mutes = sum(1 for _, ts in chat_state["mutes"].items() if ts > time.time())
    core_suffix = ""
    if s.get("core_baseline_enabled"):
        core_suffix = f" (Top {s.get('core_top_n')}, {s.get('core_reference_tf')} ref)"
    return (
        "⚙️ *Settings*\n\n"
        f"*Source:* {s['pricer']}\n"
        f"*Mode:* {_mode_label(s)}\n"
        f"*Threshold:* {s['threshold_percent']}%\n"
        f"*Poll interval:* {_fmt_interval(s['poll_interval_sec'])}\n"
        f"*TF metric:* {('OFF' if not s['show_tf_change'] else s['change_tf_label'])}\n"
        f"*Core baseline:* {'ON' if s.get('core_baseline_enabled') else 'OFF'}{core_suffix}\n"
        f"*Tracking:* {tracked_desc(s)}\n"
        f"*Muted:* {active_mutes}\n\n"
        "Ниже можно менять настройки кнопками."
    )


def _section_button(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data="st:noop")


def settings_keyboard(chat_state: dict) -> InlineKeyboardMarkup:
    s = chat_state["settings"]
    pricer = s["pricer"]
    mode = current_mode(s)
    threshold = float(s["threshold_percent"])
    interval = int(s["poll_interval_sec"])
    tf_label = s["change_tf_label"]
    tf_show = bool(s["show_tf_change"])
    core_enabled = bool(s.get("core_baseline_enabled"))
    core_top_n = int(s.get("core_top_n", 20))
    core_reference_tf = s.get("core_reference_tf", "7d")

    kb = [
        [_section_button("Источник")],
        [
            InlineKeyboardButton(_selected("Bybit", pricer == "BYBIT"), callback_data="st:src:BYBIT"),
            InlineKeyboardButton(_selected("CMC", pricer == "CMC"), callback_data="st:src:CMC"),
        ],
        [_section_button("Режим")],
        [
            InlineKeyboardButton(_selected("Top", mode == "top"), callback_data="st:mode:top"),
            InlineKeyboardButton(_selected("List", mode == "list"), callback_data="st:mode:list"),
        ],
        [_section_button("Порог алерта")],
        [
            InlineKeyboardButton(_selected("5%", threshold == 5.0), callback_data="st:thr:5"),
            InlineKeyboardButton(_selected("10%", threshold == 10.0), callback_data="st:thr:10"),
            InlineKeyboardButton(_selected("15%", threshold == 15.0), callback_data="st:thr:15"),
            InlineKeyboardButton(_selected("20%", threshold == 20.0), callback_data="st:thr:20"),
            InlineKeyboardButton(_selected("30%", threshold == 30.0), callback_data="st:thr:30"),
        ],
        [_section_button("Интервал проверки")],
        [
            InlineKeyboardButton(_selected("5m", interval == 300), callback_data="st:int:300"),
            InlineKeyboardButton(_selected("10m", interval == 600), callback_data="st:int:600"),
            InlineKeyboardButton(_selected("15m", interval == 900), callback_data="st:int:900"),
            InlineKeyboardButton(_selected("30m", interval == 1800), callback_data="st:int:1800"),
            InlineKeyboardButton(_selected("60m", interval == 3600), callback_data="st:int:3600"),
        ],
        [_section_button("TF-метрика")],
        [
            InlineKeyboardButton(_selected("OFF", not tf_show), callback_data="st:tf:off"),
            InlineKeyboardButton(_selected("5m", tf_show and tf_label == "5m"), callback_data="st:tf:5m"),
            InlineKeyboardButton(_selected("15m", tf_show and tf_label == "15m"), callback_data="st:tf:15m"),
            InlineKeyboardButton(_selected("30m", tf_show and tf_label == "30m"), callback_data="st:tf:30m"),
            InlineKeyboardButton(_selected("60m", tf_show and tf_label == "60m"), callback_data="st:tf:60m"),
        ],
        [_section_button("Core baseline")],
        [
            InlineKeyboardButton(_selected("OFF", not core_enabled), callback_data="st:core:off"),
            InlineKeyboardButton(_selected("ON", core_enabled), callback_data="st:core:on"),
        ],
        [_section_button("Core universe")],
        [
            InlineKeyboardButton(_selected("Top 20", core_top_n == 20), callback_data="st:coretop:20"),
            InlineKeyboardButton(_selected("Top 30", core_top_n == 30), callback_data="st:coretop:30"),
        ],
        [_section_button("Core reference")],
        [
            InlineKeyboardButton(_selected("1d", core_reference_tf == "1d"), callback_data="st:coretf:1d"),
            InlineKeyboardButton(_selected("3d", core_reference_tf == "3d"), callback_data="st:coretf:3d"),
            InlineKeyboardButton(_selected("7d", core_reference_tf == "7d"), callback_data="st:coretf:7d"),
            InlineKeyboardButton(_selected("30d", core_reference_tf == "30d"), callback_data="st:coretf:30d"),
        ],
    ]

    if pricer == "BYBIT":
        top_limit = int(s["bybit_top_limit"])
        kb += [
            [_section_button("Top лимит Bybit")],
            [
                InlineKeyboardButton(_selected("100", mode == "top" and top_limit == 100), callback_data="st:top:100"),
                InlineKeyboardButton(_selected("250", mode == "top" and top_limit == 250), callback_data="st:top:250"),
                InlineKeyboardButton(_selected("500", mode == "top" and top_limit == 500), callback_data="st:top:500"),
            ],
        ]
    else:
        cmc_top_limit = int(s["cmc_top_limit"])
        kb += [
            [_section_button("Top лимит CMC")],
            [
                InlineKeyboardButton(_selected("50", mode == "top" and cmc_top_limit == 50), callback_data="st:cmctop:50"),
                InlineKeyboardButton(_selected("100", mode == "top" and cmc_top_limit == 100), callback_data="st:cmctop:100"),
                InlineKeyboardButton(_selected("200", mode == "top" and cmc_top_limit == 200), callback_data="st:cmctop:200"),
            ],
        ]

    kb += [
        [_section_button("Действия")],
        [InlineKeyboardButton("ℹ️ Что значат настройки", callback_data="st:help")],
        [
            InlineKeyboardButton("📊 Status", callback_data="st:status"),
            InlineKeyboardButton("🔄 Refresh", callback_data="st:refresh"),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def alert_keyboard(source: str, sym: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔕 1h", callback_data=f"al:mute:{source}:{sym}:3600"),
                InlineKeyboardButton("🔕 24h", callback_data=f"al:mute:{source}:{sym}:86400"),
            ],
            [
                InlineKeyboardButton("♻️ Reset base", callback_data=f"al:reset:{source}:{sym}"),
                InlineKeyboardButton("📊 Status", callback_data="al:status"),
            ],
        ]
    )


def _symbols_for_status(settings: dict, quotes: Dict[str, dict]) -> List[str]:
    mode = current_mode(settings)
    if mode == "top":
        ranked = sorted(quotes.items(), key=lambda item: item[1].get("rank") or 999999)
        return [sym for sym, _ in ranked[:20]]
    if settings["pricer"] == "BYBIT":
        return settings["bybit_pairs"]
    return settings["watchlist"]


def status_text(
    chat_state: dict,
    quotes: Dict[str, dict],
    core_symbols: Optional[Set[str]] = None,
    reference_prices: Optional[Dict[str, float]] = None,
) -> str:
    """Render status message. Pure read — does NOT create baselines."""
    s = chat_state["settings"]
    unit = price_unit(s)
    now = time.time()
    core_suffix = ""
    if s.get("core_baseline_enabled"):
        core_suffix = f" (Top {s.get('core_top_n')}, {s.get('core_reference_tf')} ref)"
    lines = []
    reference_prices = reference_prices or {}
    symbols = _symbols_for_status(s, quotes)
    for sym in symbols:
        q = quotes.get(sym)
        if not q:
            continue
        key = _symbol_key(s["pricer"], sym)
        delta_pct: Optional[float] = None
        core_ref_mode = bool(s.get("core_baseline_enabled")) and is_core_symbol(sym, int(s.get("core_top_n", 20)), core_symbols)
        if core_ref_mode:
            ref_price = reference_prices.get(_asset_from_symbol(sym))
            if ref_price:
                delta_pct = ((q["price"] - ref_price) / ref_price * 100.0) if ref_price else 0.0
                delta_str = f"Δ {delta_pct:+.2f}% от {s['core_reference_tf']} ref {ref_price:.6g}"
            else:
                delta_str = f"Δ — ({s['core_reference_tf']} ref n/a)"
        else:
            base = chat_state["baselines"].get(key)
            if not base:
                delta_str = "Δ — (new)"
            else:
                base_price = float(base["price"] or 0.0)
                delta_pct = ((q["price"] - base_price) / base_price * 100.0) if base_price else 0.0
                delta_str = f"Δ {delta_pct:+.2f}% от базы {base_price:.6g}"
        muted_until = float(chat_state["mutes"].get(key, 0) or 0)
        mute_flag = " 🔕" if muted_until > now else ""
        metrics = _sub_metrics(q, s)
        rank_prefix = _rank_prefix(q)
        marker = _directional_marker(delta_pct)
        lines.append(f"{rank_prefix}{sym}: {q['price']:.6g} {unit}{mute_flag}")
        if metrics:
            lines.append(metrics)
        lines.append(f"{marker} {delta_str}")
        lines.append("")

    header = (
        "📊 *Статус бота*\n\n"
        f"*Source:* {s['pricer']} | *Mode:* {_mode_label(s)} | *Threshold:* {s['threshold_percent']}%\n"
        f"*Poll:* {_fmt_interval(s['poll_interval_sec'])} | *Cooldown:* {s['cooldown_min']} min\n"
        f"*Core baseline:* {'ON' if s.get('core_baseline_enabled') else 'OFF'}{core_suffix}\n"
        f"*Tracking:* {tracked_desc(s)}\n"
    )
    if current_mode(s) == "top":
        header += f"_Показаны top 20 из {len(quotes)} отслеживаемых_\n"
    if not lines:
        return header + "\nНет данных по текущей конфигурации."
    return header + "\n" + "\n".join(lines).rstrip()


def help_text() -> str:
    return (
        "Команды:\n"
        "/start — активировать бот для этого чата\n"
        "/status — текущее состояние\n"
        "/settings — настройки через кнопки\n"
        "/watchlist — показать текущий список / top-режим\n"
        "/setlist BTC,ETH,SOL — обновить список для list-режима\n"
        "/mute BTC 60 — отключить алерты по монете на 60 минут\n"
        "/unmute BTC — снять mute с монеты\n"
        "/unmute all — снять все mute\n"
        "/resetbase BTC — сбросить baseline по монете\n"
        "/resetbase all — сбросить все baseline для текущего источника\n"
        "/help — эта справка\n\n"
        "Как работает бот:\n"
        "- Non-core монеты работают по rolling baseline (сохранённая база в state).\n"
        "- Core монеты (Top 20/30 CMC при включённом Core baseline) работают от reference окна 1d/3d/7d/30d.\n"
        "- Алерт срабатывает по движению цены от baseline/reference, а не по 24h change.\n"
        "- TF metric (CHANGE_TF) — только дополнительная метрика, не триггер алерта.\n"
        "- Top: следим за top-N монетами источника; List: только за вашим списком.\n"
        "- /mute временно отключает алерты по монете.\n"
        "- /resetbase нужен для rolling baseline; для core reference reset не требуется.\n\n"
        "Примечания:\n"
        "- В Bybit list-режиме можно писать BTC, ETH, SOL — бот сам превратит их в BTCUSDT/ETHUSDT/...\n"
        "- В top-режиме rank показывается в /status и алертах.\n"
        "- CHANGE_TF — только дополнительная метрика, не триггер алерта."
    )


def settings_help_text() -> str:
    return (
        "ℹ️ *Что значат настройки*\n\n"
        "*Source*\n"
        "Источник котировок:\n"
        "• BYBIT — данные с Bybit\n"
        "• CMC — данные с CoinMarketCap\n\n"
        "*Mode*\n"
        "Режим отслеживания:\n"
        "• Top — следить за топ-N монетами источника\n"
        "• List — следить только за заданным списком монет\n\n"
        "*Threshold*\n"
        "Порог алерта в процентах.\n"
        "Алерт приходит, когда цена смещается на этот % от сохранённой базы (baseline), а не от 24h change.\n\n"
        "*Poll interval*\n"
        "Как часто бот проверяет цены для этого чата.\n"
        "Чем меньше интервал, тем быстрее реакция, но тем выше нагрузка и шум.\n\n"
        "*TF metric*\n"
        "Дополнительная метрика изменения за выбранный таймфрейм.\n"
        "Показывается в статусе и алертах только как справочная.\n"
        "Не является триггером алерта.\n\n"
        "*Top limit*\n"
        "Сколько монет отслеживать в режиме Top.\n"
        "Больше лимит — шире покрытие, но выше нагрузка.\n\n"
        "*Core baseline*\n"
        "Когда OFF — все монеты работают по rolling baseline.\n"
        "Когда ON — core монеты (из Core universe) работают по reference baseline.\n\n"
        "*Core universe*\n"
        "Какой размер core-рынка считать core-монетами: Top 20 или Top 30 (CMC market cap).\n\n"
        "*Core reference*\n"
        "Окно reference-базы для core-монет: 1d / 3d / 7d / 30d.\n"
        "Для core reference бот считает базу автоматически, без ручного reset.\n\n"
        "*Tracking*\n"
        "Что бот отслеживает прямо сейчас:\n"
        "• Top N\n"
        "или\n"
        "• конкретный список монет\n\n"
        "*Muted*\n"
        "Количество монет, для которых алерты временно отключены.\n\n"
        "*Важно*\n"
        "• Rolling baseline — сохранённая базовая цена для non-core\n"
        "• Core reference — автоматическая база от выбранного окна для core\n"
        "• Cooldown — пауза между повторными алертами по одной монете\n"
        "• TF metric не влияет на факт срабатывания алерта"
    )


# ---------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await update.message.reply_text("⛔ Этот чат не авторизован.")
        return
    async with _state_lock:
        chat_state = get_chat_state(chat_id)
        chat_state["enabled"] = True
        save_state()
    text = (
        "✅ Бот активирован для этого чата.\n\n"
        + settings_text(chat_state)
        + "\n\nКоманды: /status /settings /watchlist /help"
    )
    await update.message.reply_text(text, reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text())


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    chat_state = get_chat_state(update.effective_chat.id)
    await update.message.reply_text(
        settings_text(chat_state),
        reply_markup=settings_keyboard(chat_state),
        parse_mode="Markdown",
    )


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    chat_state = get_chat_state(update.effective_chat.id)
    s = chat_state["settings"]
    mode = current_mode(s)
    if mode == "top":
        if s["pricer"] == "BYBIT":
            txt = f"Текущий режим: TOP {s['bybit_top_limit']} Bybit ({s['bybit_category']})"
        else:
            txt = f"Текущий режим: TOP {s['cmc_top_limit']} CMC"
    else:
        lst = s["bybit_pairs"] if s["pricer"] == "BYBIT" else s["watchlist"]
        txt = "Текущий list-mode:\n" + (", ".join(lst) if lst else "Список пуст")
    await update.message.reply_text(txt)


async def setlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    async with _state_lock:
        chat_state = get_chat_state(update.effective_chat.id)
        s = chat_state["settings"]
        raw = update.message.text.replace("/setlist", "", 1).strip()
        if not raw:
            await update.message.reply_text("Использование: /setlist BTC,ETH,SOL")
            return
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if not items:
            await update.message.reply_text("Список пуст.")
            return
        if s["pricer"] == "BYBIT":
            s["bybit_pairs"] = [_normalize_bybit_pair(x) for x in items]
            set_mode(s, "list")
            msg = "✅ Обновил список Bybit-пар:\n" + ", ".join(s["bybit_pairs"])
        else:
            s["watchlist"] = [_normalize_cmc_symbol(x) for x in items]
            set_mode(s, "list")
            msg = "✅ Обновил CMC watchlist:\n" + ", ".join(s["watchlist"])
        chat_state["runtime"]["last_poll_ts"] = 0
        save_state()
    await update.message.reply_text(msg)


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    async with _state_lock:
        chat_state = get_chat_state(update.effective_chat.id)
        s = chat_state["settings"]
        if not context.args:
            await update.message.reply_text("Использование: /mute BTC 60")
            return
        raw_sym = context.args[0]
        minutes = 60
        if len(context.args) >= 2:
            try:
                minutes = int(context.args[1])
            except Exception:
                minutes = 60
        sym = _normalize_bybit_pair(raw_sym) if s["pricer"] == "BYBIT" else _normalize_cmc_symbol(raw_sym)
        key = _symbol_key(s["pricer"], sym)
        until = time.time() + max(1, minutes) * 60
        chat_state["mutes"][key] = until
        save_state()
    await update.message.reply_text(f"🔕 {sym} muted на {minutes} min.")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    async with _state_lock:
        chat_state = get_chat_state(update.effective_chat.id)
        s = chat_state["settings"]
        if not context.args:
            await update.message.reply_text("Использование: /unmute BTC  или  /unmute all")
            return
        raw = context.args[0].strip().lower()
        if raw == "all":
            removed = []
            prefix = f"{s['pricer']}:"
            for key in list(chat_state["mutes"].keys()):
                if key.startswith(prefix):
                    removed.append(key)
                    del chat_state["mutes"][key]
            save_state()
            await update.message.reply_text(f"✅ Снял mute: {len(removed)} шт.")
            return
        sym = _normalize_bybit_pair(raw) if s["pricer"] == "BYBIT" else _normalize_cmc_symbol(raw)
        key = _symbol_key(s["pricer"], sym)
        if key in chat_state["mutes"]:
            del chat_state["mutes"][key]
            save_state()
            await update.message.reply_text(f"✅ Снял mute с {sym}.")
        else:
            await update.message.reply_text(f"Для {sym} mute не найден.")


async def resetbase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /resetbase BTC  или  /resetbase all")
        return
    raw = context.args[0].strip().lower()
    async with _state_lock:
        chat_state = get_chat_state(update.effective_chat.id)
        s_snapshot = dict(chat_state["settings"])

    if raw == "all":
        async with _state_lock:
            chat_state = get_chat_state(update.effective_chat.id)
            prefix = f"{chat_state['settings']['pricer']}:"
            removed = []
            for key in list(chat_state["baselines"].keys()):
                if key.startswith(prefix):
                    removed.append(key)
                    del chat_state["baselines"][key]
            save_state()
        await update.message.reply_text(f"♻️ Сбросил baseline: {len(removed)} шт.")
        return

    sym = _normalize_bybit_pair(raw) if s_snapshot["pricer"] == "BYBIT" else _normalize_cmc_symbol(raw)
    if s_snapshot.get("core_baseline_enabled"):
        loop = asyncio.get_running_loop()
        core_symbols = await loop.run_in_executor(_fetch_pool, get_cached_core_universe, int(s_snapshot.get("core_top_n", 20)))
        if is_core_symbol(sym, int(s_snapshot.get("core_top_n", 20)), core_symbols):
            await update.message.reply_text(
                f"ℹ️ Для {sym} включён core reference mode: baseline считается автоматически ({s_snapshot.get('core_reference_tf')}). "
                "Команда reset не нужна."
            )
            return

    async with _state_lock:
        chat_state = get_chat_state(update.effective_chat.id)
        key = _symbol_key(chat_state["settings"]["pricer"], sym)
        if key in chat_state["baselines"]:
            del chat_state["baselines"][key]
            save_state()
            await update.message.reply_text(f"♻️ Baseline для {sym} сброшен. Новый будет создан на следующем poll/status.")
        else:
            await update.message.reply_text(f"Для {sym} baseline не найден.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    chat_state = get_chat_state(update.effective_chat.id)
    s = chat_state["settings"]
    try:
        quotes = await _fetch_async(s)
        core_symbols, reference_prices = await _prepare_core_context(s, quotes)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка запроса цен: {e}")
        return
    text = status_text(chat_state, quotes, core_symbols=core_symbols, reference_prices=reference_prices)
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_authorized(query.message.chat_id):
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    if action == "noop":
        return
    if action == "help":
        await query.message.reply_text(settings_help_text(), parse_mode="Markdown")
        return

    async with _state_lock:
        chat_state = get_chat_state(query.message.chat_id)
        s = chat_state["settings"]

        if action == "src" and len(parts) >= 3:
            target = parts[2].upper()
            if target == "CMC" and not CMC_API_KEY:
                await query.answer("CMC_API_KEY отсутствует в окружении", show_alert=True)
                return
            s["pricer"] = target
            chat_state["runtime"]["last_poll_ts"] = 0
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "mode" and len(parts) >= 3:
            set_mode(s, parts[2])
            chat_state["runtime"]["last_poll_ts"] = 0
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "thr" and len(parts) >= 3:
            s["threshold_percent"] = float(parts[2])
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "int" and len(parts) >= 3:
            s["poll_interval_sec"] = int(parts[2])
            chat_state["runtime"]["last_poll_ts"] = 0
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "tf" and len(parts) >= 3:
            if parts[2].lower() == "off":
                s["show_tf_change"] = False
            else:
                label, bybit_val = _parse_change_tf(parts[2])
                s["show_tf_change"] = True
                s["change_tf_label"] = label
                s["change_tf_bybit"] = bybit_val
            chat_state["runtime"]["last_poll_ts"] = 0
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "core" and len(parts) >= 3:
            s["core_baseline_enabled"] = parts[2].lower() == "on"
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "coretop" and len(parts) >= 3:
            top_n = int(parts[2])
            if top_n in (20, 30):
                s["core_top_n"] = top_n
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "coretf" and len(parts) >= 3:
            tf = parts[2].lower()
            if tf in CORE_REFERENCE_TF_TO_SEC:
                s["core_reference_tf"] = tf
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "top" and len(parts) >= 3:
            s["bybit_top_limit"] = int(parts[2])
            s["pricer"] = "BYBIT"
            set_mode(s, "top")
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

        if action == "cmctop" and len(parts) >= 3:
            if not CMC_API_KEY:
                await query.answer("CMC_API_KEY отсутствует в окружении", show_alert=True)
                return
            s["cmc_top_limit"] = int(parts[2])
            s["pricer"] = "CMC"
            set_mode(s, "top")
            save_state()
            await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
            return

    # --- actions that need fetch (outside state lock) ---
    if action == "status":
        chat_state = get_chat_state(query.message.chat_id)
        try:
            quotes = await _fetch_async(chat_state["settings"])
            core_symbols, reference_prices = await _prepare_core_context(chat_state["settings"], quotes)
            txt = status_text(chat_state, quotes, core_symbols=core_symbols, reference_prices=reference_prices)
            await query.message.reply_text(txt, parse_mode="Markdown")
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка статуса: {e}")
        return

    if action == "refresh":
        chat_state = get_chat_state(query.message.chat_id)
        await query.edit_message_text(settings_text(chat_state), reply_markup=settings_keyboard(chat_state), parse_mode="Markdown")
        return


async def alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_authorized(query.message.chat_id):
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]

    if action == "status":
        chat_state = get_chat_state(query.message.chat_id)
        try:
            quotes = await _fetch_async(chat_state["settings"])
            core_symbols, reference_prices = await _prepare_core_context(chat_state["settings"], quotes)
            txt = status_text(chat_state, quotes, core_symbols=core_symbols, reference_prices=reference_prices)
            await query.message.reply_text(txt, parse_mode="Markdown")
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка статуса: {e}")
        return

    if action == "mute" and len(parts) >= 5:
        async with _state_lock:
            chat_state = get_chat_state(query.message.chat_id)
            source = parts[2].upper()
            sym = parts[3].upper()
            seconds = int(parts[4])
            key = _symbol_key(source, sym)
            until = time.time() + seconds
            chat_state["mutes"][key] = until
            save_state()
        await query.answer(f"{sym} muted", show_alert=False)
        await query.message.reply_text(
            f"🔕 {sym} muted на {seconds // 3600 if seconds >= 3600 else seconds // 60} {'h' if seconds >= 3600 else 'min'}."
        )
        return

    if action == "reset" and len(parts) >= 4:
        source = parts[2].upper()
        sym = parts[3].upper()
        chat_state = get_chat_state(query.message.chat_id)
        s = chat_state["settings"]
        if source == s["pricer"] and s.get("core_baseline_enabled"):
            loop = asyncio.get_running_loop()
            core_symbols = await loop.run_in_executor(_fetch_pool, get_cached_core_universe, int(s.get("core_top_n", 20)))
            if is_core_symbol(sym, int(s.get("core_top_n", 20)), core_symbols):
                await query.answer("Core reference mode", show_alert=False)
                await query.message.reply_text(
                    f"ℹ️ Для {sym} baseline считается автоматически от {s.get('core_reference_tf')} reference. Reset не нужен."
                )
                return
        async with _state_lock:
            chat_state = get_chat_state(query.message.chat_id)
            key = _symbol_key(source, sym)
            if key in chat_state["baselines"]:
                del chat_state["baselines"][key]
                save_state()
        await query.answer("Baseline reset", show_alert=False)
        await query.message.reply_text(f"♻️ Baseline для {sym} сброшен. Новый будет создан на следующем poll/status.")
        return


# ---------------------------------------------------------------
# Polling engine
# ---------------------------------------------------------------
async def poll_engine_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    state_dirty = False

    async with _state_lock:
        chat_ids = list(state["chats"].keys())

    for chat_id in chat_ids:
        try:
            async with _state_lock:
                chat_state = state["chats"].get(chat_id)
                if not chat_state:
                    continue

                # --- whitelist check in poll loop ---
                if not _is_authorized(int(chat_id)):
                    continue
                if not chat_state.get("enabled", True):
                    continue

                s = chat_state["settings"]
                last_poll_ts = float(chat_state.get("runtime", {}).get("last_poll_ts", 0) or 0)
                if now - last_poll_ts < int(s["poll_interval_sec"]):
                    continue

                chat_state["runtime"]["last_poll_ts"] = now
                state_dirty = True
                s_snapshot = dict(s)

            try:
                quotes = await _fetch_async(s_snapshot)
            except Exception as e:
                logging.warning(f"[chat {chat_id}] Fetch error: {e}")
                continue
            core_symbols, reference_prices = await _prepare_core_context(s_snapshot, quotes)

            unit = price_unit(s_snapshot)
            cooldown = int(s_snapshot["cooldown_min"]) * 60
            symbols = list(quotes.keys())
            alerts_to_send = []

            async with _state_lock:
                chat_state = state["chats"].get(chat_id)
                if not chat_state:
                    continue
                s = chat_state["settings"]
                for sym in symbols:
                    q = quotes.get(sym)
                    if not q:
                        continue
                    key = _symbol_key(s["pricer"], sym)
                    core_ref_mode = bool(s.get("core_baseline_enabled")) and is_core_symbol(sym, int(s.get("core_top_n", 20)), core_symbols)
                    mute_until = float(chat_state["mutes"].get(key, 0) or 0)
                    if mute_until > now:
                        continue
                    if core_ref_mode:
                        ref_price = reference_prices.get(_asset_from_symbol(sym))
                        if not ref_price:
                            continue
                        move_pct = ((q["price"] - ref_price) / ref_price * 100.0) if ref_price else 0.0
                        base = chat_state["baselines"].get(key) or {}
                        last_alert_ts = float(base.get("last_alert", 0) or 0)
                        if abs(move_pct) >= float(s["threshold_percent"]) and (now - last_alert_ts) >= cooldown:
                            alerts_to_send.append((sym, q, move_pct, ref_price, "reference"))
                            chat_state["baselines"][key] = {"price": base.get("price", q["price"]), "ts": now, "last_alert": now}
                            state_dirty = True
                    else:
                        base = chat_state["baselines"].get(key)
                        if not base:
                            chat_state["baselines"][key] = {"price": q["price"], "ts": now, "last_alert": 0}
                            state_dirty = True
                            continue
                        base_price = float(base.get("price") or 0.0)
                        move_pct = ((q["price"] - base_price) / base_price * 100.0) if base_price else 0.0
                        last_alert_ts = float(base.get("last_alert", 0) or 0)
                        if abs(move_pct) >= float(s["threshold_percent"]) and (now - last_alert_ts) >= cooldown:
                            alerts_to_send.append((sym, q, move_pct, base_price, "rolling"))
                            chat_state["baselines"][key] = {"price": q["price"], "ts": now, "last_alert": now}
                            state_dirty = True

            for sym, q, move_pct, base_price, baseline_mode in alerts_to_send:
                rank_prefix = _rank_prefix(q)
                metrics = _sub_metrics(q, s)
                marker = _directional_marker(move_pct)
                if baseline_mode == "reference":
                    delta_line = f"{marker} Δ {move_pct:+.2f}% от {s['core_reference_tf']} ref {base_price:.6g}"
                else:
                    delta_line = f"{marker} Δ {move_pct:+.2f}% от базы {base_price:.6g}"
                text = (
                    f"🚨 {rank_prefix}{sym}\n"
                    f"Цена: {q['price']:.6g} {unit}\n"
                )
                if metrics:
                    text += f"{metrics}\n"
                text += delta_line
                try:
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                        reply_markup=alert_keyboard(s["pricer"], sym),
                    )
                except Exception as e:
                    logging.warning(f"[chat {chat_id}] Send message failed: {e}")

        except Exception as e:
            logging.warning(f"[chat {chat_id}] Unexpected poll error: {e}")
            state_dirty = True

    # single save per engine tick
    if state_dirty:
        async with _state_lock:
            save_state()


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main():
    load_state()
    logging.info(
        "Booting CryptoWatcher (Sprint 1.1): "
        f"default_source={DEFAULT_PRICER}; "
        f"default_threshold={DEFAULT_THRESHOLD_PERCENT}%; "
        f"default_interval={_fmt_interval(DEFAULT_POLL_INTERVAL_SEC)}; "
        f"engine_interval={_fmt_interval(ENGINE_INTERVAL_SEC)}; "
        f"whitelist={'ON (' + str(len(ALLOWED_CHAT_IDS)) + ')' if ALLOWED_CHAT_IDS else 'OFF'}"
    )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("setlist", setlist_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("resetbase", resetbase_cmd))

    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^st:"))
    app.add_handler(CallbackQueryHandler(alert_callback, pattern=r"^al:"))

    app.job_queue.run_repeating(poll_engine_job, interval=ENGINE_INTERVAL_SEC, first=5)

    app.run_polling()


if __name__ == "__main__":
    main()
