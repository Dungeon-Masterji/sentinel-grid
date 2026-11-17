"""Thin Groq (OpenAI-compatible) chat client with retries, a SQLite cache and a daily
token ledger.

    narrate(messages)                 -> str, raises LLMUnavailable
    narrate_with_fallback(messages, fallback_text, alert_id=...)
                                      -> {"text", "source": "llm" | "template_fallback", ...}

Plain ``requests`` (no SDK) so the API can vendor this file without new dependencies
(Azure F1 storage quota). Models come from the environment:

    GROQ_API_KEY     required; without it every call raises LLMUnavailable (-> template)
    GROQ_MODEL       narration + actions (default openai/gpt-oss-120b)
    GROQ_MODEL_AR    Arabic translation (default allam-2-7b)

Cache: data/processed/explain/narration_cache.sqlite (override: $NARRATION_CACHE_PATH).
Tables: ``narration`` (alert_id, kind, prompt_hash) -> text, ``actions`` (key) -> JSON
with created_at for TTLs, ``usage`` (UTC day) -> tokens. prune() keeps it small: rows
older than 7 days are dropped. Fallback text is never cached, so the next run retries.

Unlike the fusion package, this module reads the wall clock: token budgets and cache
TTLs are about real-world spend, not the replay time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("sentinel.explain.groq")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_MODEL_AR = "allam-2-7b"
DEFAULT_DAILY_BUDGET = 100_000
MAX_RETRIES = 3                 # retries after the first attempt, on 429 / 5xx / network
BACKOFF_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 20.0
KEEP_DAYS = 7


def _default_cache_path() -> Path:
    env = os.environ.get("NARRATION_CACHE_PATH")
    if env:
        return Path(env)
    # packages/pipelines/explain/groq_client.py -> repo root
    return Path(__file__).resolve().parents[3] / "data" / "processed" / "explain" / "narration_cache.sqlite"


def model_en() -> str:
    return os.environ.get("GROQ_MODEL") or DEFAULT_MODEL


def model_ar() -> str:
    return os.environ.get("GROQ_MODEL_AR") or DEFAULT_MODEL_AR


class LLMUnavailable(RuntimeError):
    """No key, offline, rate-limited after retries, budget exhausted, or bad response."""


class LLMOffline(LLMUnavailable):
    """No key, or the service kept failing after retries: stop calling for this run."""


class BudgetExceeded(LLMOffline):
    pass


def prompt_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------------------
# cache + usage ledger
# ---------------------------------------------------------------------------------------
class NarrationCache:
    def __init__(self, path: Path | str | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.path = str(path) if path is not None else str(_default_cache_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS narration (
                alert_id TEXT, kind TEXT, prompt_hash TEXT, text TEXT, model TEXT,
                created_at TEXT, PRIMARY KEY (alert_id, kind, prompt_hash));
            CREATE TABLE IF NOT EXISTS actions (
                key TEXT PRIMARY KEY, payload TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS usage (
                day TEXT PRIMARY KEY, tokens INTEGER, calls INTEGER);
        """)
        self._db.commit()

    def now(self) -> datetime:
        return self._now()

    def _today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def get(self, alert_id: str, kind: str, phash: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT text FROM narration WHERE alert_id=? AND kind=? AND prompt_hash=?",
                (alert_id, kind, phash)).fetchone()
        return row[0] if row else None

    def put(self, alert_id: str, kind: str, phash: str, text: str, model: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO narration VALUES (?,?,?,?,?,?)",
                             (alert_id, kind, phash, text, model, self.now().isoformat()))
            self._db.commit()

    def get_action(self, key: str, max_age_hours: float) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT payload, created_at FROM actions WHERE key=?",
                                   (key,)).fetchone()
        if not row:
            return None
        if self.now() - datetime.fromisoformat(row[1]) > timedelta(hours=max_age_hours):
            return None
        return json.loads(row[0])

    def put_action(self, key: str, payload: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO actions VALUES (?,?,?)",
                             (key, json.dumps(payload, ensure_ascii=False), self.now().isoformat()))
            self._db.commit()

    def tokens_today(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT tokens FROM usage WHERE day=?", (self._today(),)).fetchone()
        return int(row[0]) if row else 0

    def add_usage(self, tokens: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO usage VALUES (?,?,1) ON CONFLICT(day) DO UPDATE SET "
                "tokens = tokens + excluded.tokens, calls = calls + 1",
                (self._today(), int(tokens)))
            self._db.commit()

    def prune(self, keep_days: int = KEEP_DAYS) -> None:
        cut = (self.now() - timedelta(days=keep_days)).isoformat()
        with self._lock:
            self._db.execute("DELETE FROM narration WHERE created_at < ?", (cut,))
            self._db.execute("DELETE FROM actions WHERE created_at < ?", (cut,))
            self._db.execute("DELETE FROM usage WHERE day < ?",
                             ((self.now() - timedelta(days=2 * keep_days)).strftime("%Y-%m-%d"),))
            self._db.commit()
            if self.path != ":memory:":
                self._db.execute("VACUUM")

    def close(self) -> None:
        self._db.close()


_default_cache: NarrationCache | None = None


def default_cache() -> NarrationCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = NarrationCache()
    return _default_cache


# ---------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------
@dataclass
class LLMReply:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _retry_after(resp) -> float | None:
    v = resp.headers.get("retry-after") if resp is not None else None
    try:
        return min(float(v), MAX_RETRY_AFTER_S) if v is not None else None
    except ValueError:
        return None


def chat(messages: list[dict], *, model: str | None = None, max_tokens: int = 400,
         temperature: float = 0.3, json_mode: bool = False, timeout: float = 30.0,
         cache: NarrationCache | None = None, daily_budget: int | None = None,
         sleep: Callable[[float], None] = time.sleep, session=None) -> LLMReply:
    """One chat completion. Retries 3x with exponential backoff on 429 / 5xx / network
    errors; raises LLMUnavailable (or BudgetExceeded) instead of returning junk."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMOffline("GROQ_API_KEY not set")
    model = model or model_en()
    cache = cache or default_cache()
    budget = int(daily_budget if daily_budget is not None
                 else os.environ.get("GROQ_DAILY_TOKEN_BUDGET", DEFAULT_DAILY_BUDGET))
    used = cache.tokens_today()
    if used >= budget:
        raise BudgetExceeded(f"daily token budget spent ({used} >= {budget})")

    body = {"model": model, "messages": messages, "max_tokens": int(max_tokens),
            "temperature": float(temperature)}
    if model.startswith("openai/gpt-oss"):
        # reasoning models: reasoning tokens count against max_tokens; keep them few
        body.update(reasoning_effort="low", include_reasoning=False)
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    import requests
    http = session or requests
    url = (os.environ.get("GROQ_BASE_URL") or GROQ_BASE_URL).rstrip("/") + "/chat/completions"
    last = "no attempt"
    for attempt in range(MAX_RETRIES + 1):
        resp = None
        try:
            resp = http.post(url, json=body, timeout=timeout,
                             headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as e:
            last = f"{e.__class__.__name__}: {e}"
        if resp is not None:
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage") or {}
                cache.add_usage(int(usage.get("total_tokens") or 0))
                choice = (data.get("choices") or [{}])[0]
                text = ((choice.get("message") or {}).get("content") or "").strip()
                if not text:
                    raise LLMUnavailable(f"empty completion (finish_reason="
                                         f"{choice.get('finish_reason')})")
                return LLMReply(text=text, model=data.get("model", model),
                                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                                completion_tokens=int(usage.get("completion_tokens") or 0))
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code != 429 and resp.status_code < 500:
                raise LLMUnavailable(last)        # 4xx other than 429: retrying won't help
        if attempt < MAX_RETRIES:
            wait = _retry_after(resp) or BACKOFF_S[attempt] * (1 + 0.25 * random.random())
            log.info("groq %s (attempt %d/%d); retrying in %.1fs", last[:80], attempt + 1,
                     MAX_RETRIES + 1, wait)
            sleep(wait)
    raise LLMOffline(f"gave up after {MAX_RETRIES + 1} attempts: {last}")


def narrate(prompt_messages: list[dict], model: str | None = None, max_tokens: int = 400,
            temperature: float = 0.3, *, alert_id: str | None = None, kind: str = "narration",
            cache_key=None, cache: NarrationCache | None = None, force: bool = False,
            **kw) -> str:
    """Response text for ``prompt_messages``. With ``alert_id`` the result is cached by
    (alert_id, kind, prompt_hash): ``cache_key`` if given (a stable summary of what the
    text depends on), else the messages themselves. ``force`` skips the cache read."""
    return narrate_reply(prompt_messages, model, max_tokens, temperature, alert_id=alert_id,
                         kind=kind, cache_key=cache_key, cache=cache, force=force, **kw).text


def narrate_reply(prompt_messages: list[dict], model: str | None = None, max_tokens: int = 400,
                  temperature: float = 0.3, *, alert_id: str | None = None,
                  kind: str = "narration", cache_key=None, cache: NarrationCache | None = None,
                  force: bool = False, **kw) -> LLMReply:
    model = model or model_en()
    cache = cache or default_cache()
    phash = prompt_hash({"model": model, "key": cache_key if cache_key is not None else prompt_messages})
    if alert_id and not force:
        hit = cache.get(alert_id, kind, phash)
        if hit is not None:
            return LLMReply(text=hit, model=model, cached=True)
    reply = chat(prompt_messages, model=model, max_tokens=max_tokens, temperature=temperature,
                 cache=cache, **kw)
    if alert_id:
        cache.put(alert_id, kind, phash, reply.text, reply.model)
    return reply


def narrate_with_fallback(prompt_messages: list[dict], fallback_text: str, **kw) -> dict:
    """narrate(), but never raises: on LLMUnavailable returns the template text tagged
    ``source: 'template_fallback'``."""
    try:
        r = narrate_reply(prompt_messages, **kw)
        return {"text": r.text, "source": "llm", "model": r.model, "cached": r.cached,
                "tokens": r.total_tokens}
    except LLMUnavailable as e:
        log.warning("narration fell back to template: %s", e)
        return {"text": fallback_text, "source": "template_fallback", "model": None,
                "cached": False, "tokens": 0, "error": str(e)[:200]}
