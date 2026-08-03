"""
e2e_multi_user.py -- Phase 3 multi-user end-to-end test: 3 parallel users, 20 turns each.

Tests the full pipeline (engine.process()) with:
  - Real Bitrix24 API  (settings.bitrix_webhook)
  - Real OpenAI API    (settings.openai_api_key)
  - Real Redis DB 5    (redis://localhost:6379/5)

Three users run SIMULTANEOUSLY via asyncio.gather:
  - chat_id 80001: Focuses on OmikronAvto deal (fleet management, budget 700k)
  - chat_id 80002: Focuses on EtaFinans deal   (billing 500k, risks, stages)
  - chat_id 80003: Focuses on TetaMedia deal   (video platform, deadline April 1)

After all users finish, STATE ISOLATION is verified:
  - Each chat_id has its own Redis state
  - Critical facts from 80002/80003 do not appear in 80001's state

Results are saved to e2e_multi_user_results.json.

Run:
    cd /path/to/ai-native-crm
    PYTHONIOENCODING=utf-8 python e2e_multi_user.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

# ---------------------------------------------------------------------------
# Bootstrap: sys.path + env before any project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/5")

# Suppress verbose library logging so the turn-by-turn output stays readable
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
# Keep our own module at INFO so critical pipeline messages still appear
logging.getLogger("ai_native_crm").setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Project imports (after sys.path is patched)
# ---------------------------------------------------------------------------
import redis.asyncio as aioredis

from ai_native_crm.adapters.bitrix import BitrixAdapter
from ai_native_crm.config import settings
from ai_native_crm.core.action_router import ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.core.engine import AgentEngine
from ai_native_crm.core.response_validator import ResponseValidator
from ai_native_crm.core.state_store import StateStore
from ai_native_crm.services.llm_client import LLMClient
from ai_native_crm.services.lock import DistributedLock
from ai_native_crm.services.metrics import MetricsService
from ai_native_crm.services.pii_anonymizer import PIIAnonymizer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_IDS: list[int] = [80001, 80002, 80003]

RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "e2e_multi_user_results.json",
)

# gpt-4o-mini pricing (USD per 1 000 000 tokens)
_PRICE_INPUT_PER_1M = 0.15
_PRICE_OUTPUT_PER_1M = 0.60

INTER_TURN_DELAY_SEC = 0.5  # avoid OpenAI rate-limiting between turns per user

# ---------------------------------------------------------------------------
# Per-user 20-turn scenarios (transliterated Russian)
# ---------------------------------------------------------------------------

# chat_id 80001: OmikronAvto -- fleet management, budget 700k
TURNS_80001: list[str] = [
    "Pokaji vse moi sdelki",                                                               # 1
    "Rasskaji podrobnee pro sdelku OmikronAvto -- fleet management",                       # 2
    "Klient OmikronAvto skazal chto byudzhet maksimum 700000 rubley. Zafiksiruy eto.",     # 3
    "Kakie sdelki samye krupnye po summe?",                                                # 4
    "Kakie sdelki v stadii NEW?",                                                          # 5
    "Perevedi sdelku OmikronAvto v stadiyu UC_QUALIFIED",                                  # 6
    "Napomni mne pozvonitj po OmikronAvto cherez chas",                                    # 7
    "Klient OmikronAvto podtverdil chto oni sravnivayut s konkurentom. Zafiksiruy.",       # 8
    "Kakey byudzhet u OmikronAvto? Napomni.",                                              # 9
    "Sdelaj otchyot po voronke -- skolyko sdelok na kazhdoy stadii",                       # 10
    "Kakie sdelki blizki k zakrytiyu?",                                                    # 11
    "Perevedi OmikronAvto v stadiyu UC_INVOICE",                                           # 12
    "Klient OmikronAvto gotov podpisatj na sleduyushchey nedele. Zafiksiruy.",             # 13
    "Pokaji vse critical facts po OmikronAvto",                                            # 14
    "Kakaya obshchaya summa vsekh aktivnykh sdelok?",                                      # 15
    "Zakroj sdelku OmikronAvto kak vyigrannuyu",                                          # 16
    "Pokaji metriki kachestva",                                                             # 17
    "Chto mne delatj v pervuyu ocheredj sejchas?",                                         # 18
    "Vspomni byudzhet OmikronAvto i podtverjdi chto sdelka zakryta",                       # 19
    "Spasibo! Podvedi itog po rabote s OmikronAvto.",                                      # 20
]

# chat_id 80002: EtaFinans -- billing 500k, risks, reminders
TURNS_80002: list[str] = [
    "Pokaji vse moi sdelki",                                                               # 1
    "Rasskaji pro sdelku EtaFinans -- billing",                                            # 2
    "Kakie riski est po sdelke EtaFinans?",                                                # 3
    "Klient EtaFinans poprosil skidku 10%. Stoit li davatj? Zafiksiruy prosbu.",          # 4
    "Perevedi EtaFinans v stadiyu UC_QUALIFIED",                                           # 5
    "Napomni mne obsudite kontrakt s EtaFinans cherez 2 chasa",                            # 6
    "Kakie sdelki na stadii UC_QUALIFIED sejchas?",                                        # 7
    "Klient EtaFinans podtverdil byudzhet 500000. Zafiksiruy.",                            # 8
    "Obsudim strategiyu po EtaFinans -- kakie sleduushchie shagi?",                        # 9
    "Perevedi EtaFinans v stadiyu UC_INVOICE",                                             # 10
    "Klient EtaFinans zaprosil detalnyy schet. Kakie dokumenty nuzhny?",                  # 11
    "Pokaji vse critical facts po EtaFinans",                                              # 12
    "Klient EtaFinans zaderjivaet oplatu -- prosit eshche nedelyu. Zafiksiruy.",           # 13
    "Kakie sdelki v stadii UC_INVOICE sejchas?",                                           # 14
    "Esli EtaFinans ne oplatit cherez nedelyu, kakie deystviya?",                          # 15
    "Klient EtaFinans podtverdil oplatu. Perevedi v stadiyu UC_PAYMENT.",                  # 16
    "Pokaji metriki po rabote s EtaFinans",                                                # 17
    "Sdelaj otchyot -- chto proishodilo s EtaFinans za vse vremya",                        # 18
    "Vspomni byudzhet EtaFinans i status sdelki",                                          # 19
    "Spasibo! Podvedi itog po sdelke EtaFinans.",                                          # 20
]

# chat_id 80003: TetaMedia -- video platform, deadline April 1
TURNS_80003: list[str] = [
    "Pokaji vse moi sdelki",                                                               # 1
    "Rasskaji pro sdelku TetaMedia -- video platforma",                                    # 2
    "Klient TetaMedia skazal: dedlayn 1 aprelya, posle etogo kontrakt otmenyaetsya. Zafiksiruy.",  # 3
    "Kakie sdelki imeyut zhestkie dedlayni?",                                              # 4
    "Perevedi TetaMedia v stadiyu UC_QUALIFIED",                                           # 5
    "Napomni mne svyazatsya s TetaMedia za 3 dnya do dedlayna",                            # 6
    "Klient TetaMedia khochet dobavitj modul translyaciy. Kak eto vliyaet na byudzhet?",  # 7
    "Perevedi TetaMedia v stadiyu UC_INVOICE",                                             # 8
    "Klient TetaMedia podtverdil finalnyy byudzhet. Zafiksiruy summuy sdelki.",            # 9
    "Kakie sdelki blizki k dedlaynu 1 aprelya?",                                          # 10
    "Pokaji vse critical facts po TetaMedia",                                              # 11
    "Klient TetaMedia prosit otlozhitj dedlayn na 2 aprelya. Chto delat?",                # 12
    "Dedlayn po TetaMedia ostaetsya 1 aprelya -- klient podtverdil. Zafiksiruy.",          # 13
    "Kakie dokumenty nuzhno podgotovitj dlya zakrytiya TetaMedia?",                        # 14
    "Perevedi TetaMedia v stadiyu UC_PAYMENT",                                             # 15
    "Klient TetaMedia oplatil. Zakroj sdelku kak vyigrannuyu.",                            # 16
    "Pokaji metriki kachestva po TetaMedia",                                               # 17
    "Sdelaj otchyot -- istoriya sdelki TetaMedia ot nachala do konca",                    # 18
    "Vspomni dedlayn TetaMedia i podtverjdi chto sdelka zakryta",                         # 19
    "Spasibo! Podvedi itog po rabote s TetaMedia.",                                        # 20
]

assert len(TURNS_80001) == 20, f"Expected 20 turns for 80001, got {len(TURNS_80001)}"
assert len(TURNS_80002) == 20, f"Expected 20 turns for 80002, got {len(TURNS_80002)}"
assert len(TURNS_80003) == 20, f"Expected 20 turns for 80003, got {len(TURNS_80003)}"

# Map chat_id -> turns list
TURNS_BY_CHAT: dict[int, list[str]] = {
    80001: TURNS_80001,
    80002: TURNS_80002,
    80003: TURNS_80003,
}

# Human-readable label for each scenario
SCENARIO_LABEL: dict[int, str] = {
    80001: "OmikronAvto (fleet, budget 700k)",
    80002: "EtaFinans (billing 500k, risks)",
    80003: "TetaMedia (video platform, deadline Apr 1)",
}

# ---------------------------------------------------------------------------
# FakeBot: no-op Telegram sender for testing
# ---------------------------------------------------------------------------


class FakeBot:
    """Drop-in replacement for the Telegram bot — discards all messages silently."""

    async def send_message(self, chat_id: int, text: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_print(msg: str) -> None:
    """Print with ASCII-safe fallback (cp1251 safe, no box chars)."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def _percentile(values: list[float], pct: float) -> float:
    """Return the p-th percentile of a sorted list."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    hi = lo + 1
    if hi >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] + (k - lo) * (sorted_vals[hi] - sorted_vals[lo])


async def _flush_redis_state(redis_client: aioredis.Redis, chat_id: int) -> None:
    """
    Delete all Redis keys that belong to this chat_id before the run.

    Keys flushed:
        state:{chat_id}
        critical_facts:{chat_id}
        audit:{chat_id}
        metrics:{chat_id}
        reminders:{chat_id}
        pii:{chat_id}
        lock:chat:{chat_id}
    """
    keys_to_delete = [
        f"state:{chat_id}",
        f"critical_facts:{chat_id}",
        f"audit:{chat_id}",
        f"metrics:{chat_id}",
        f"reminders:{chat_id}",
        f"pii:{chat_id}",
        f"lock:chat:{chat_id}",
    ]
    deleted = await redis_client.delete(*keys_to_delete)
    _safe_print(f"[FLUSH] Deleted {deleted} Redis keys for chat_id={chat_id}")


# ---------------------------------------------------------------------------
# Component factory
# ---------------------------------------------------------------------------


def _build_engine(
    redis_client: aioredis.Redis,
    crm: BitrixAdapter,
) -> tuple[AgentEngine, StateStore]:
    """
    Wire all real components together and return (AgentEngine, StateStore).

    The StateStore is returned separately so the caller can read back
    per-turn metadata from Redis after each turn.
    """
    store = StateStore(redis_client, audit_ttl_days=settings.audit_ttl_days)
    llm = LLMClient()
    validator = ResponseValidator(crm)
    router = ActionRouter(crm=crm, bot=None, state_store=store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(crm)
    anonymizer = PIIAnonymizer(redis_client)
    lock = DistributedLock(redis_client)
    metrics = MetricsService(state_store=store, bot=None, alert_chat_id=None)

    engine = AgentEngine(
        state_store=store,
        crm=crm,
        llm=llm,
        validator=validator,
        action_router=router,
        compressor=compressor,
        drift=drift,
        anonymizer=anonymizer,
        lock=lock,
        metrics=metrics,
    )
    return engine, store


# ---------------------------------------------------------------------------
# Per-turn metadata collection
# ---------------------------------------------------------------------------


async def _collect_turn_meta(
    store: StateStore,
    chat_id: int,
    turn_number: int,
    user_input: str,
    response: str,
    latency_ms: int,
    error: str | None,
) -> dict:
    """
    Read Redis state/audit after a turn and build the per-turn metrics dict.
    """
    tokens_in = 0
    tokens_out = 0
    state_size_bytes = 0
    critical_facts_count = 0
    iteration = 0
    working_memory_len = 0

    try:
        raw_state = await store.redis.get(f"state:{chat_id}")
        if raw_state:
            state_size_bytes = len(raw_state.encode("utf-8"))
            try:
                state_obj = json.loads(raw_state)
                iteration = int(state_obj.get("iteration", 0))
                working_memory_len = len(state_obj.get("working_memory", ""))
            except (json.JSONDecodeError, ValueError):
                pass

        cf_count = await store.redis.llen(f"critical_facts:{chat_id}")
        critical_facts_count = int(cf_count) if cf_count else 0

        audit_raw = await store.redis.xrevrange(f"audit:{chat_id}", count=1)
        if audit_raw:
            _, fields = audit_raw[0]
            tokens_in = int(fields.get("tokens_in", 0))
            tokens_out = int(fields.get("tokens_out", 0))

    except Exception as exc:
        logging.getLogger(__name__).warning(
            "_collect_turn_meta error chat_id=%d: %s", chat_id, exc
        )

    return {
        "turn": turn_number,
        "user_input": user_input,
        "response": response,
        "latency_total_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "state_size_bytes": state_size_bytes,
        "critical_facts_count": critical_facts_count,
        "iteration": iteration,
        "error": error,
        "working_memory_len": working_memory_len,
    }


# ---------------------------------------------------------------------------
# Single-user runner (runs all 20 turns for one chat_id)
# ---------------------------------------------------------------------------


async def _run_user(
    chat_id: int,
    turns: list[str],
    engine: AgentEngine,
    store: StateStore,
) -> list[dict]:
    """
    Run all turns for a single user sequentially and return per-turn metrics.

    This coroutine is designed to be launched concurrently with other users
    via asyncio.gather — it does not share engine or store with other users.
    """
    total_turns = len(turns)
    metrics_per_turn: list[dict] = []
    label = SCENARIO_LABEL.get(chat_id, str(chat_id))

    _safe_print(f"[USER {chat_id}] Starting {total_turns} turns -- {label}")

    for idx, user_input in enumerate(turns):
        turn_number = idx + 1
        t_start = time.monotonic()
        error_msg: str | None = None
        response = ""

        try:
            response = await engine.process(user_input, chat_id)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            response = f"[ERROR: {error_msg}]"
            logging.getLogger(__name__).error(
                "[USER %d] Turn %d raised unhandled exception: %s",
                chat_id, turn_number, exc, exc_info=True,
            )

        latency_ms = round((time.monotonic() - t_start) * 1000)

        turn_data = await _collect_turn_meta(
            store=store,
            chat_id=chat_id,
            turn_number=turn_number,
            user_input=user_input,
            response=response,
            latency_ms=latency_ms,
            error=error_msg,
        )
        metrics_per_turn.append(turn_data)

        status = "ERROR" if error_msg else "OK"
        _safe_print(
            f"  [USER {chat_id}] Turn {turn_number:02d}/{total_turns} [{status}] "
            f"{latency_ms}ms | "
            f"tokens={turn_data['tokens_in']}in/{turn_data['tokens_out']}out | "
            f"facts={turn_data['critical_facts_count']} | "
            f"iter={turn_data['iteration']}"
        )

        preview = response[:100].replace("\n", " ")
        _safe_print(f"             -> {preview}")

        # Small delay between turns to avoid OpenAI rate-limiting
        if idx < total_turns - 1:
            await asyncio.sleep(INTER_TURN_DELAY_SEC)

    _safe_print(f"[USER {chat_id}] All {total_turns} turns complete.")
    return metrics_per_turn


# ---------------------------------------------------------------------------
# Post-run per-user summary builder
# ---------------------------------------------------------------------------


async def _build_user_summary(
    redis_client: aioredis.Redis,
    chat_id: int,
    metrics_per_turn: list[dict],
) -> dict:
    """
    Read final Redis state for chat_id and build a complete summary dict.
    """
    # Final state
    final_state_raw = await redis_client.get(f"state:{chat_id}")
    final_state: dict = {}
    if final_state_raw:
        try:
            final_state = json.loads(final_state_raw)
        except json.JSONDecodeError:
            pass

    final_iteration = int(final_state.get("iteration", 0))
    final_wm = final_state.get("working_memory", "")
    final_assessment = final_state.get("agent_assessment", "")
    final_summary = final_state.get("conversation_summary", "")
    final_state_size = len(final_state_raw.encode("utf-8")) if final_state_raw else 0

    # Critical facts
    cf_raw = await redis_client.lrange(f"critical_facts:{chat_id}", 0, -1)
    critical_facts_list: list[dict] = []
    for item in cf_raw:
        try:
            critical_facts_list.append(json.loads(item))
        except json.JSONDecodeError:
            critical_facts_list.append({"raw": item})

    # Redis metrics hash
    metrics_raw = await redis_client.hgetall(f"metrics:{chat_id}")
    total_turns_redis = int(metrics_raw.get("total_turns", 0))
    hallucination_count = int(metrics_raw.get("hallucination_count", 0))
    action_total = int(metrics_raw.get("action_total", 0))
    action_success = int(metrics_raw.get("action_success", 0))
    hallucination_rate = hallucination_count / total_turns_redis if total_turns_redis else 0.0
    action_success_rate = action_success / action_total if action_total else 0.0

    # Audit stream length
    audit_len = await redis_client.xlen(f"audit:{chat_id}")

    # Aggregate latency / token stats from per-turn metrics
    latencies = [m["latency_total_ms"] for m in metrics_per_turn]
    total_tokens_in = sum(m["tokens_in"] for m in metrics_per_turn)
    total_tokens_out = sum(m["tokens_out"] for m in metrics_per_turn)
    errors_in_turns = [m for m in metrics_per_turn if m["error"]]

    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    min_latency = min(latencies) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0
    p95_latency = _percentile(latencies, 95)

    # Cost estimate (USD)
    cost_input = (total_tokens_in / 1_000_000) * _PRICE_INPUT_PER_1M
    cost_output = (total_tokens_out / 1_000_000) * _PRICE_OUTPUT_PER_1M
    total_cost = cost_input + cost_output

    # All facts as a single string for keyword checks
    all_facts_text = " ".join(cf.get("content", "") for cf in critical_facts_list).lower()

    return {
        "chat_id": chat_id,
        "scenario": SCENARIO_LABEL.get(chat_id, str(chat_id)),
        "summary": {
            "turns_run": len(metrics_per_turn),
            "errors_total": len(errors_in_turns),
            "latency_avg_ms": round(avg_latency),
            "latency_min_ms": round(min_latency),
            "latency_max_ms": round(max_latency),
            "latency_p95_ms": round(p95_latency),
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "cost_usd_estimate": round(total_cost, 6),
            "hallucination_rate": round(hallucination_rate, 4),
            "hallucination_count": hallucination_count,
            "action_total": action_total,
            "action_success": action_success,
            "action_success_rate": round(action_success_rate, 4),
            "final_state_size_bytes": final_state_size,
            "critical_facts_count": len(critical_facts_list),
            "final_iteration": final_iteration,
            "audit_entries": int(audit_len),
        },
        "metrics_per_turn": metrics_per_turn,
        "critical_facts": critical_facts_list,
        "final_state": {
            "iteration": final_iteration,
            "state_size_bytes": final_state_size,
            "working_memory_len": len(final_wm),
            "working_memory_preview": final_wm[:500],
            "assessment_preview": final_assessment[:300],
            "summary_preview": final_summary[:300],
        },
        "redis_metrics_raw": dict(metrics_raw),
        "all_facts_text": all_facts_text,  # used internally for isolation check
        "errors": [{"turn": m["turn"], "error": m["error"]} for m in errors_in_turns],
    }


# ---------------------------------------------------------------------------
# State isolation check
# ---------------------------------------------------------------------------


def _check_isolation(user_summaries: dict[int, dict]) -> dict:
    """
    Verify that critical facts from 80002/80003 did NOT leak into 80001's state.

    Checks both the working_memory and critical_facts content of chat_id 80001
    for keywords that are exclusive to the other two users' scenarios.

    Returns an isolation report dict.
    """
    # Gather searchable text for chat_id 80001
    s1 = user_summaries.get(80001, {})
    facts_text_1 = s1.get("all_facts_text", "").lower()
    wm_preview_1 = s1.get("final_state", {}).get("working_memory_preview", "").lower()
    full_text_1 = facts_text_1 + " " + wm_preview_1

    # Keywords that are unique to 80002 (EtaFinans) and 80003 (TetaMedia)
    # and should NOT appear in 80001's state
    etafinans_keywords = ["etafinans", "eta finans", "billing 500", "500000"]
    tetamedia_keywords = ["tetamedia", "teta media", "dedlayn 1 aprelya", "video platforma"]

    leaked_from_80002: list[str] = [kw for kw in etafinans_keywords if kw in full_text_1]
    leaked_from_80003: list[str] = [kw for kw in tetamedia_keywords if kw in full_text_1]

    # For a cleaner check also verify the opposite: 80001-specific terms exist only in 80001
    omikron_keywords = ["omikron", "700000", "fleet"]
    omikron_in_1 = any(kw in full_text_1 for kw in omikron_keywords)

    facts_text_2 = user_summaries.get(80002, {}).get("all_facts_text", "").lower()
    facts_text_3 = user_summaries.get(80003, {}).get("all_facts_text", "").lower()
    omikron_leaked_to_2 = any(kw in facts_text_2 for kw in omikron_keywords)
    omikron_leaked_to_3 = any(kw in facts_text_3 for kw in omikron_keywords)

    isolation_ok = (
        len(leaked_from_80002) == 0
        and len(leaked_from_80003) == 0
        and not omikron_leaked_to_2
        and not omikron_leaked_to_3
    )

    return {
        "isolation_ok": isolation_ok,
        "leaked_from_80002_into_80001": leaked_from_80002,
        "leaked_from_80003_into_80001": leaked_from_80003,
        "omikron_present_in_80001": omikron_in_1,
        "omikron_leaked_to_80002": omikron_leaked_to_2,
        "omikron_leaked_to_80003": omikron_leaked_to_3,
    }


# ---------------------------------------------------------------------------
# Main e2e runner
# ---------------------------------------------------------------------------


async def run_e2e_multi() -> None:
    _safe_print("=" * 70)
    _safe_print("  E2E MULTI-USER TEST -- 3 USERS x 20 TURNS (PARALLEL)")
    _safe_print(f"  chat_ids   : {CHAT_IDS}")
    _safe_print(f"  redis_url  : {settings.redis_url}")
    _safe_print(f"  llm_model  : {settings.llm_model}")
    _safe_print(f"  bitrix     : {settings.bitrix_webhook[:40]}...")
    _safe_print("=" * 70)

    # ------------------------------------------------------------------
    # 1. Connect to real Redis (DB 5 from env / settings)
    # ------------------------------------------------------------------
    redis_url = os.environ.get("REDIS_URL", settings.redis_url)
    redis_client: aioredis.Redis = aioredis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_connect_timeout,
    )

    try:
        await redis_client.ping()
        _safe_print(f"[OK] Redis connected: {redis_url}")
    except Exception as exc:
        _safe_print(f"[FATAL] Cannot connect to Redis: {exc}")
        await redis_client.aclose()
        return

    # ------------------------------------------------------------------
    # 2. Flush previous state for all 3 chat_ids
    # ------------------------------------------------------------------
    _safe_print("\n[SETUP] Flushing Redis state for all users ...")
    for cid in CHAT_IDS:
        await _flush_redis_state(redis_client, cid)

    # ------------------------------------------------------------------
    # 3. Build one engine per user (each gets its own components to avoid
    #    cross-user state contamination through shared in-memory objects)
    # ------------------------------------------------------------------
    _safe_print("\n[SETUP] Building engines ...")

    # All users share the same CRM adapter (stateless HTTP client)
    crm = BitrixAdapter(settings.bitrix_webhook)

    engines_and_stores: dict[int, tuple[AgentEngine, StateStore]] = {}
    for cid in CHAT_IDS:
        engine, store = _build_engine(redis_client, crm)
        engines_and_stores[cid] = (engine, store)
        _safe_print(f"  [OK] Engine ready for chat_id={cid} -- {SCENARIO_LABEL[cid]}")

    # ------------------------------------------------------------------
    # 4. Run all 3 users simultaneously via asyncio.gather
    # ------------------------------------------------------------------
    wall_start = time.monotonic()
    _safe_print(f"\n[START] Launching all 3 users in parallel ...\n")

    results_80001, results_80002, results_80003 = await asyncio.gather(
        _run_user(
            chat_id=80001,
            turns=TURNS_BY_CHAT[80001],
            engine=engines_and_stores[80001][0],
            store=engines_and_stores[80001][1],
        ),
        _run_user(
            chat_id=80002,
            turns=TURNS_BY_CHAT[80002],
            engine=engines_and_stores[80002][0],
            store=engines_and_stores[80002][1],
        ),
        _run_user(
            chat_id=80003,
            turns=TURNS_BY_CHAT[80003],
            engine=engines_and_stores[80003][0],
            store=engines_and_stores[80003][1],
        ),
    )

    wall_elapsed_sec = time.monotonic() - wall_start
    _safe_print(f"\n[DONE] All users finished in {wall_elapsed_sec:.1f}s wall time.")

    # ------------------------------------------------------------------
    # 5. Build per-user summaries from Redis
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 70)
    _safe_print("  POST-RUN ANALYSIS")
    _safe_print("=" * 70)

    per_user_results: dict[int, dict] = {}
    raw_results_map = {
        80001: results_80001,
        80002: results_80002,
        80003: results_80003,
    }

    for cid in CHAT_IDS:
        per_user_results[cid] = await _build_user_summary(
            redis_client=redis_client,
            chat_id=cid,
            metrics_per_turn=raw_results_map[cid],
        )

    # ------------------------------------------------------------------
    # 6. Print per-user summary
    # ------------------------------------------------------------------
    for cid in CHAT_IDS:
        s = per_user_results[cid]
        sm = s["summary"]
        label = s["scenario"]
        _safe_print(f"\n  --- USER {cid} : {label} ---")
        _safe_print(f"  Turns run        : {sm['turns_run']}")
        _safe_print(f"  Errors           : {sm['errors_total']}")
        _safe_print(f"  Latency avg/p95  : {sm['latency_avg_ms']}ms / {sm['latency_p95_ms']}ms")
        _safe_print(f"  Tokens in/out    : {sm['total_tokens_in']:,} / {sm['total_tokens_out']:,}")
        _safe_print(f"  Cost estimate    : ${sm['cost_usd_estimate']:.4f}")
        _safe_print(f"  Hallucinations   : {sm['hallucination_count']} ({sm['hallucination_rate']:.1%})")
        _safe_print(f"  Action success   : {sm['action_success']}/{sm['action_total']} ({sm['action_success_rate']:.1%})")
        _safe_print(f"  Critical facts   : {sm['critical_facts_count']}")
        _safe_print(f"  Final state size : {sm['final_state_size_bytes']} bytes")
        _safe_print(f"  Final iteration  : {sm['final_iteration']}")
        _safe_print(f"  Audit entries    : {sm['audit_entries']}")

        # Print critical facts (cap at 10 per user)
        cf_list = s["critical_facts"]
        _safe_print(f"  Critical facts ({len(cf_list)} total):")
        for i, cf in enumerate(cf_list[:10]):
            content = cf.get("content", cf.get("raw", "?"))[:80]
            fact_type = cf.get("fact_type", "?")
            deal_id = cf.get("deal_id", "")
            _safe_print(f"    [{i+1:02d}] [{fact_type}] deal={deal_id or '-'}: {content}")
        if len(cf_list) > 10:
            _safe_print(f"    ... and {len(cf_list) - 10} more")

        if s["errors"]:
            _safe_print(f"  Errors in turns:")
            for e in s["errors"]:
                _safe_print(f"    Turn {e['turn']:02d}: {e['error']}")

    # ------------------------------------------------------------------
    # 7. State isolation verification
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 70)
    _safe_print("  STATE ISOLATION CHECK")
    _safe_print("=" * 70)

    isolation = _check_isolation(per_user_results)

    _safe_print(f"  Isolation OK               : {isolation['isolation_ok']}")
    _safe_print(f"  OmikronAvto in 80001       : {isolation['omikron_present_in_80001']}")
    _safe_print(f"  EtaFinans leaked -> 80001  : {isolation['leaked_from_80002_into_80001'] or 'none'}")
    _safe_print(f"  TetaMedia leaked -> 80001  : {isolation['leaked_from_80003_into_80001'] or 'none'}")
    _safe_print(f"  OmikronAvto leaked -> 80002: {isolation['omikron_leaked_to_80002']}")
    _safe_print(f"  OmikronAvto leaked -> 80003: {isolation['omikron_leaked_to_80003']}")

    if isolation["isolation_ok"]:
        _safe_print("\n  [PASS] State isolation verified -- no cross-user leakage detected.")
    else:
        _safe_print("\n  [FAIL] State isolation VIOLATED -- see leak details above!")

    # ------------------------------------------------------------------
    # 8. Aggregate statistics across all users
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 70)
    _safe_print("  AGGREGATE STATISTICS (all 3 users)")
    _safe_print("=" * 70)

    all_latencies: list[float] = []
    total_tokens_in_all = 0
    total_tokens_out_all = 0
    total_errors_all = 0
    total_hallucinations_all = 0
    total_turns_all = 0
    total_facts_all = 0
    total_cost_all = 0.0

    for cid in CHAT_IDS:
        sm = per_user_results[cid]["summary"]
        per_user_latencies = [m["latency_total_ms"] for m in raw_results_map[cid]]
        all_latencies.extend(per_user_latencies)
        total_tokens_in_all += sm["total_tokens_in"]
        total_tokens_out_all += sm["total_tokens_out"]
        total_errors_all += sm["errors_total"]
        total_hallucinations_all += sm["hallucination_count"]
        total_turns_all += sm["turns_run"]
        total_facts_all += sm["critical_facts_count"]
        total_cost_all += sm["cost_usd_estimate"]

    avg_lat_all = sum(all_latencies) / len(all_latencies) if all_latencies else 0.0
    p95_lat_all = _percentile(all_latencies, 95)
    hallucination_rate_all = total_hallucinations_all / total_turns_all if total_turns_all else 0.0

    _safe_print(f"  Total turns across all users : {total_turns_all}")
    _safe_print(f"  Total errors                 : {total_errors_all}")
    _safe_print(f"  Wall time elapsed            : {wall_elapsed_sec:.1f}s")
    _safe_print(f"  Latency avg (all turns)      : {avg_lat_all:.0f}ms")
    _safe_print(f"  Latency p95 (all turns)      : {p95_lat_all:.0f}ms")
    _safe_print(f"  Total tokens in              : {total_tokens_in_all:,}")
    _safe_print(f"  Total tokens out             : {total_tokens_out_all:,}")
    _safe_print(f"  Total cost estimate          : ${total_cost_all:.4f}")
    _safe_print(f"  Hallucination rate (all)     : {hallucination_rate_all:.1%} ({total_hallucinations_all}/{total_turns_all})")
    _safe_print(f"  Total critical facts         : {total_facts_all}")

    # ------------------------------------------------------------------
    # 9. Save results JSON
    # ------------------------------------------------------------------

    # Remove internal-only fields before serialization
    serializable_users: dict = {}
    for cid in CHAT_IDS:
        entry = dict(per_user_results[cid])
        entry.pop("all_facts_text", None)  # internal only
        serializable_users[str(cid)] = entry

    results = {
        "run_meta": {
            "phase": "Phase 3 - Multi-User Parallel",
            "chat_ids": CHAT_IDS,
            "turns_per_user": 20,
            "redis_url": redis_url,
            "llm_model": settings.llm_model,
            "inter_turn_delay_sec": INTER_TURN_DELAY_SEC,
            "wall_time_sec": round(wall_elapsed_sec, 2),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "aggregate": {
            "total_turns": total_turns_all,
            "total_errors": total_errors_all,
            "total_tokens_in": total_tokens_in_all,
            "total_tokens_out": total_tokens_out_all,
            "total_cost_usd_estimate": round(total_cost_all, 6),
            "latency_avg_ms": round(avg_lat_all),
            "latency_p95_ms": round(p95_lat_all),
            "hallucination_rate": round(hallucination_rate_all, 4),
            "total_hallucinations": total_hallucinations_all,
            "total_critical_facts": total_facts_all,
            "wall_time_sec": round(wall_elapsed_sec, 2),
        },
        "isolation": isolation,
        "users": serializable_users,
    }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    _safe_print(f"\n[DONE] Results saved to: {RESULTS_FILE}")
    _safe_print("=" * 70)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    await crm.close()
    await redis_client.aclose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run_e2e_multi())
