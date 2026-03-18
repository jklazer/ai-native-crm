"""
Минимальная веб-панель AI-Native CRM.
FastAPI + встроенный HTML (без React — лишняя сложность для MVP).

Запуск: uvicorn ai_native_crm.web.app:app --reload --port 8080
Авторизация: header X-API-Key (простой ключ из настроек).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ai_native_crm.config import settings
from ai_native_crm.core.state_store import StateStore

logger = logging.getLogger(__name__)

app = FastAPI(title="AI-Native CRM Panel", version="1.0.0", docs_url="/docs")

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

API_KEY_HEADER = "X-API-Key"


@app.middleware("http")
async def check_api_key(request: Request, call_next: Any) -> Any:
    # Dashboard is publicly readable — no key required
    if request.url.path == "/":
        return await call_next(request)
    key = request.headers.get(API_KEY_HEADER, "")
    if key != settings.web_api_key:
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup() -> None:
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.store = StateStore(app.state.redis)
    logger.info("Web panel started. Redis: %s", settings.redis_url)


@app.on_event("shutdown")
async def shutdown() -> None:
    await app.state.redis.aclose()
    logger.info("Web panel shutdown. Redis connection closed.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_secret(value: str) -> str:
    """Mask an API key so only the last 4 chars are visible."""
    if not value:
        return ""
    visible = value[-4:] if len(value) >= 4 else value
    return f"sk-...{visible}"


async def _scan_state_keys(redis: aioredis.Redis) -> list[str]:
    """SCAN for all state:{chat_id} keys. Non-blocking even on large datasets."""
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor=cursor, match="state:*", count=200)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


# ---------------------------------------------------------------------------
# Dashboard — GET /
# ---------------------------------------------------------------------------

_DASHBOARD_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; font-weight: 600; color: #f0f6fc; margin-bottom: 4px; }
.subtitle { color: #8b949e; font-size: 13px; margin-bottom: 24px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-bottom: 28px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
.card-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #8b949e; margin-bottom: 6px; }
.card-value { font-size: 26px; font-weight: 700; color: #f0f6fc; }
.card-value.green { color: #3fb950; }
.card-value.red { color: #f85149; }
.card-value.yellow { color: #d29922; }
.section-title { font-size: 15px; font-weight: 600; color: #f0f6fc; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #8b949e; padding: 8px 12px; border-bottom: 1px solid #21262d; }
td { padding: 10px 12px; border-bottom: 1px solid #21262d; vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #1c2129; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500; }
.badge-green { background: #1a3a2a; color: #3fb950; }
.badge-red { background: #3a1a1a; color: #f85149; }
.badge-gray { background: #21262d; color: #8b949e; }
.note { font-size: 12px; color: #8b949e; margin-top: 20px; }
"""


def _build_dashboard_html(
    active_chats: list[dict[str, Any]],
    total_chats: int,
    redis_ok: bool,
) -> str:
    status_class = "green" if redis_ok else "red"
    status_text = "Online" if redis_ok else "Redis offline"

    rows_html = ""
    for chat in active_chats:
        chat_id = chat["chat_id"]
        iteration = chat.get("iteration", 0)
        last_updated = chat.get("last_updated", "—")
        facts_count = chat.get("facts_count", 0)

        rows_html += f"""
        <tr>
            <td><code>{chat_id}</code></td>
            <td>{iteration}</td>
            <td style="font-size:12px;color:#8b949e">{last_updated}</td>
            <td>{facts_count}</td>
            <td>
                <a href="/api/state/{chat_id}">state</a> &nbsp;
                <a href="/api/facts/{chat_id}">facts</a> &nbsp;
                <a href="/api/metrics/{chat_id}">metrics</a> &nbsp;
                <a href="/api/audit/{chat_id}">audit</a>
            </td>
        </tr>"""

    if not rows_html:
        rows_html = """
        <tr><td colspan="5" style="text-align:center;color:#8b949e;padding:24px">
            Нет активных чатов
        </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-Native CRM Panel</title>
  <style>{_DASHBOARD_CSS}</style>
</head>
<body>
  <div class="container">
    <h1>AI-Native CRM</h1>
    <div class="subtitle">Web panel · API key required for /api/* endpoints</div>

    <div class="cards">
      <div class="card">
        <div class="card-label">Redis</div>
        <div class="card-value {status_class}">{status_text}</div>
      </div>
      <div class="card">
        <div class="card-label">Active Chats</div>
        <div class="card-value">{total_chats}</div>
      </div>
      <div class="card">
        <div class="card-label">CRM Adapter</div>
        <div class="card-value" style="font-size:18px">{settings.crm_adapter}</div>
      </div>
      <div class="card">
        <div class="card-label">LLM Model</div>
        <div class="card-value" style="font-size:14px;padding-top:6px">{settings.llm_model}</div>
      </div>
    </div>

    <div class="section-title">Active Chat Sessions</div>
    <table>
      <thead>
        <tr>
          <th>Chat ID</th>
          <th>Iteration</th>
          <th>Last Updated</th>
          <th>Facts</th>
          <th>Links</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>

    <div class="note">
      Endpoints: GET /api/config · GET /api/state/{{id}} · GET /api/facts/{{id}} ·
      GET /api/metrics/{{id}} · GET /api/audit/{{id}} · POST /api/reset/{{id}}?confirm=yes
    </div>
  </div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard: bot status, list of active sessions, key metrics summary."""
    redis: aioredis.Redis = request.app.state.redis
    store: StateStore = request.app.state.store

    # Check Redis connectivity
    redis_ok = False
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        pass

    active_chats: list[dict[str, Any]] = []
    if redis_ok:
        state_keys = await _scan_state_keys(redis)
        for key in state_keys:
            # key = "state:{chat_id}"
            try:
                chat_id = int(key.split(":", 1)[1])
            except (IndexError, ValueError):
                continue

            state = await store.load(chat_id)
            facts = await store.get_critical_facts(chat_id)
            active_chats.append(
                {
                    "chat_id": chat_id,
                    "iteration": state.iteration,
                    "last_updated": state.last_updated or "—",
                    "facts_count": len(facts),
                }
            )

    # Sort by iteration descending so the most active chats appear first
    active_chats.sort(key=lambda c: c["iteration"], reverse=True)

    html = _build_dashboard_html(active_chats, len(active_chats), redis_ok)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    """Return current configuration. Secrets are masked."""
    return {
        "crm_adapter": settings.crm_adapter,
        "token_budget": settings.token_budget,
        "llm_model": settings.llm_model,
        "llm_fallback_model": settings.llm_fallback_model,
        "llm_temperature": settings.llm_temperature,
        "llm_max_tokens": settings.llm_max_tokens,
        "pii_enabled": settings.pii_enabled,
        "pii_ttl_sec": settings.pii_ttl_sec,
        "drift_check_interval": settings.drift_check_interval,
        "drift_threshold": settings.drift_threshold,
        "hallucination_threshold": settings.hallucination_threshold,
        "action_success_threshold": settings.action_success_threshold,
        "audit_ttl_days": settings.audit_ttl_days,
        "reminder_check_interval": settings.reminder_check_interval,
        "lock_timeout_sec": settings.lock_timeout_sec,
        "redis_url": settings.redis_url,
        # Secrets masked — never expose raw key material
        "openai_api_key": _mask_secret(settings.openai_api_key),
        "anthropic_api_key": _mask_secret(settings.anthropic_api_key),
    }


# ---------------------------------------------------------------------------
# POST /api/config
# ---------------------------------------------------------------------------

# Fields that are safe to update at runtime — no restarts, no secrets
_SAFE_CONFIG_FIELDS: set[str] = {
    "crm_adapter",
    "token_budget",
    "llm_model",
    "pii_enabled",
}

_VALID_CRM_ADAPTERS = {"mock", "bitrix", "amo"}


@app.post("/api/config")
async def update_config(body: dict[str, Any]) -> dict[str, Any]:
    """
    Update a subset of runtime config fields.
    Only fields in _SAFE_CONFIG_FIELDS are accepted; others are rejected.
    """
    applied: dict[str, Any] = {}
    rejected: dict[str, str] = {}

    for field, value in body.items():
        if field not in _SAFE_CONFIG_FIELDS:
            rejected[field] = "field not allowed"
            continue

        # Validate each field individually before applying
        if field == "crm_adapter":
            if value not in _VALID_CRM_ADAPTERS:
                rejected[field] = f"must be one of {sorted(_VALID_CRM_ADAPTERS)}"
                continue
            settings.crm_adapter = value

        elif field == "token_budget":
            if not isinstance(value, int) or value < 100:
                rejected[field] = "must be an integer >= 100"
                continue
            settings.token_budget = value

        elif field == "llm_model":
            if not isinstance(value, str) or not value.strip():
                rejected[field] = "must be a non-empty string"
                continue
            settings.llm_model = value.strip()

        elif field == "pii_enabled":
            if not isinstance(value, bool):
                rejected[field] = "must be a boolean"
                continue
            settings.pii_enabled = value

        applied[field] = value

    return {"applied": applied, "rejected": rejected}


# ---------------------------------------------------------------------------
# GET /api/state/{chat_id}
# ---------------------------------------------------------------------------


@app.get("/api/state/{chat_id}")
async def get_state(chat_id: int, request: Request) -> dict[str, Any]:
    """Return SemanticState for a chat. 404 if no state has been persisted yet."""
    store: StateStore = request.app.state.store
    redis: aioredis.Redis = request.app.state.redis

    # Check if the key actually exists — StateStore.load() returns an empty state
    # even when there is no Redis key, so we must check explicitly.
    key = f"state:{chat_id}"
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(status_code=404, detail=f"No state for chat_id={chat_id}")

    state = await store.load(chat_id)
    return dataclasses.asdict(state)


# ---------------------------------------------------------------------------
# GET /api/facts/{chat_id}
# ---------------------------------------------------------------------------


@app.get("/api/facts/{chat_id}")
async def get_facts(chat_id: int, request: Request) -> list[dict[str, Any]]:
    """Return all critical facts for a chat (append-only list)."""
    store: StateStore = request.app.state.store
    facts = await store.get_critical_facts(chat_id)
    return [dataclasses.asdict(f) for f in facts]


# ---------------------------------------------------------------------------
# GET /api/audit/{chat_id}
# ---------------------------------------------------------------------------


@app.get("/api/audit/{chat_id}")
async def get_audit(chat_id: int, request: Request) -> list[dict[str, Any]]:
    """Return last 50 audit entries for a chat."""
    store: StateStore = request.app.state.store
    return await store.get_audit(chat_id, limit=50)


# ---------------------------------------------------------------------------
# GET /api/metrics/{chat_id}
# ---------------------------------------------------------------------------


@app.get("/api/metrics/{chat_id}")
async def get_metrics(chat_id: int, request: Request) -> dict[str, Any]:
    """Return raw metrics dict plus computed rates."""
    redis: aioredis.Redis = request.app.state.redis
    key = f"metrics:{chat_id}"
    raw: dict[str, str] = await redis.hgetall(key)

    def _int(field: str) -> int:
        val = raw.get(field)
        return int(val) if val is not None else 0

    total_turns = _int("total_turns")
    hallucination_count = _int("hallucination_count")
    action_total = _int("action_total")
    action_success = _int("action_success")

    hallucination_rate = hallucination_count / total_turns if total_turns else 0.0
    action_success_rate = action_success / action_total if action_total else 0.0

    return {
        "chat_id": chat_id,
        "total_turns": total_turns,
        "hallucination_count": hallucination_count,
        "hallucination_rate": round(hallucination_rate, 4),
        "action_total": action_total,
        "action_success": action_success,
        "action_success_rate": round(action_success_rate, 4),
        # Threshold breach indicators
        "hallucination_alert": hallucination_rate > settings.hallucination_threshold,
        "action_success_alert": (
            action_total > 0 and action_success_rate < settings.action_success_threshold
        ),
    }


# ---------------------------------------------------------------------------
# POST /api/reset/{chat_id}
# ---------------------------------------------------------------------------


@app.post("/api/reset/{chat_id}")
async def reset_chat(
    chat_id: int,
    request: Request,
    confirm: str = Query(default=""),
) -> dict[str, Any]:
    """
    Delete all Redis data for a chat_id.
    Requires ?confirm=yes to prevent accidental data loss.
    Deletes: state:{id}, critical_facts:{id}, metrics:{id}
    """
    if confirm.lower() != "yes":
        raise HTTPException(
            status_code=400,
            detail="Add ?confirm=yes to confirm deletion. This cannot be undone.",
        )

    redis: aioredis.Redis = request.app.state.redis

    keys_to_delete = [
        f"state:{chat_id}",
        f"critical_facts:{chat_id}",
        f"metrics:{chat_id}",
    ]

    deleted: list[str] = []
    for key in keys_to_delete:
        count = await redis.delete(key)
        if count:
            deleted.append(key)

    return {
        "chat_id": chat_id,
        "deleted": deleted,
        "not_found": [k for k in keys_to_delete if k not in deleted],
    }
