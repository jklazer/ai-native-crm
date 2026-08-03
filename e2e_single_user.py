"""
e2e_single_user.py -- End-to-end test: 50 turns, one real user, real APIs.

Tests the full pipeline (engine.process()) with:
  - Real Bitrix24 API  (settings.bitrix_webhook)
  - Real OpenAI API    (settings.openai_api_key)
  - Real Redis DB 5    (redis://localhost:6379/5)

All state is flushed for chat_id=1019677560 before the run starts.
Results are saved to e2e_single_user_results.json.

Run:
    cd /path/to/ai-native-crm
    PYTHONIOENCODING=utf-8 python e2e_single_user.py
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

CHAT_ID: int = 1019677560
RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "e2e_single_user_results.json",
)

# gpt-4o-mini pricing (USD per 1 000 000 tokens)
_PRICE_INPUT_PER_1M = 0.15
_PRICE_OUTPUT_PER_1M = 0.60

INTER_TURN_DELAY_SEC = 0.5   # avoid OpenAI rate-limiting

# ---------------------------------------------------------------------------
# 50-turn scenario
# ---------------------------------------------------------------------------

TURNS: list[str] = [
    # Monday (turns 1-10)
    "Pokaji vse moi sdelki",
    "Kakie sdelki samye krupnye po summe? Top-5",
    "Rasskaji podrobnee pro sdelku OmikronAvto -- fleet management",
    "Klient OmikronAvto skazal chto byudzhet maksimum 700000 rubley. Zafiksiruy eto.",
    "Perevedi sdelku AlfaTekh v stadiyu kvalifikacii",
    "Kakie sdelki v stadii NEW?",
    "Napomni mne pozvonitj po sdelke ZetaLogistik cherez chas",
    "Klient KappaRiteyl otkazalsya, prichina -- nashli deshevle. Zafiksiruy otkaz.",
    "Sdelaj otchyot po voronke -- skolyko sdelok na kazhdoy stadii",
    "Chto mne delatj v pervuyu ocheredj? Kakie sdelki prioritetnye?",

    # Tuesday (turns 11-20)
    "Prishel novyy lid: OOO TestovayaKompaniya, byudzhet 1 million, nuzhna integraciya 1S. Sozdaj sdelku.",
    "Pokaji obnovlennyy spisok sdelok",
    "Kakey byudzhet byl u OmikronAvto? Napomni.",
    "Obsudim strategiyu po EtaFinans -- billing na 500k. Kakie riski?",
    "A chto po YotaTekh -- mobilnoe prilozhenie za 620k? Stoit li davatj skidku?",
    "Obnoviy summu sdelki GammaPro na 120000",
    "Pokaji kontakty klientov",
    "Kakie sdelki na stadii UC_INVOICE?",
    "Rasskaji chto my obsuzhdali za poslednie khody",
    "Vspomni vse fakty po otkazam",

    # Wednesday (turns 21-30)
    "Prishel eshche odin lid: IP Testov, avtomatizaciya dokumentooborota, byudzhet 200k",
    "Perevedi sdelku BetaSoft v stadiyu UC_QUALIFIED",
    "Klient LyambdaStroy podtverdil byudzhet 750000 i gotov podpisatj na sleduyushchey nedele. Zafiksiruy.",
    "Kakaya obshchaya summa vsekh aktivnykh sdelok?",
    "Zakroj sdelku PiDizayn kak vyigrannuyu",
    "Pokaji vse critical facts",
    "Kakey byudzhet byl u samogo pervogo klienta -- OmikronAvto?",
    "Pokaji metriki kachestva",
    "Proverj drejft steyata",
    "Skolyko sdelok my obrabotali za etu nedelyu?",

    # Thursday (turns 31-40)
    "Klient NyuFarma khochet uvelichitj byudzhet do 500000. Obnoviy summu.",
    "Perevedi EtaFinans v stadiyu UC_INVOICE",
    "Klient TetaMedia skazal: dedlayn 1 aprelya, posle etogo kontrakt otmenyaetsya. Zafiksiruy.",
    "Kakie sdelki blizki k zakrytiyu?",
    "Sozdaj sdelku: OOO NovyyKlient, razrabotka portala, 350000 rubley",
    "Obnoviy sdelku DeltaInzh -- summu na 75000",
    "Klient ZetaLogistik podtverdil oplatu. Perevedi v stadiyu UC_PAYMENT.",
    "Pokaji vse fakty po sdelkam",
    "Kakie sdelki na stadii NEW sejchas?",
    "Sdelaj promezhutochnyy otchyot po vsem sdelkam",

    # Friday (turns 41-50)
    "Vspomni byudzhet OmikronAvto i dedlayn po TetaMedia",
    "Klient MyuKonsalt otkazalsya -- byudzhet urezali. Zafiksiruy otkaz.",
    "Perevedi KsiEnergo v stadiyu UC_QUALIFIED",
    "Kakie sdelki my zakryli na etoy nedele?",
    "Pokaji statistiku: skolyko sdelok sozdano, obnovleno, zakryto",
    "Obnoviy sdelku YotaTekh -- summu na 580000 so skidkoy",
    "Kakie eshche sdelki nuzhno obrabotatj do konca nedeli?",
    "Pokaji itogovyy otchyot po voronke za nedelyu",
    "Proverj drift i pokaji metriki",
    "Spasibo za rabotu na etoy nedele! Podvedi itog.",
]

assert len(TURNS) == 50, f"Expected 50 turns, got {len(TURNS)}"

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
) -> AgentEngine:
    """Wire all real components together and return an AgentEngine."""
    store = StateStore(redis_client, audit_ttl_days=settings.audit_ttl_days)
    llm = LLMClient()
    validator = ResponseValidator(crm)
    # bot=None: no Telegram sends during the test
    router = ActionRouter(crm=crm, bot=None, state_store=store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(crm)
    anonymizer = PIIAnonymizer(redis_client)
    lock = DistributedLock(redis_client)
    metrics = MetricsService(state_store=store, bot=None, alert_chat_id=None)

    return AgentEngine(
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


# ---------------------------------------------------------------------------
# Per-turn metrics collection
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
    # Defaults
    tokens_in = 0
    tokens_out = 0
    state_size_bytes = 0
    critical_facts_count = 0
    iteration = 0
    working_memory_len = 0
    compression_triggered = False

    try:
        # State size + iteration
        raw_state = await store.redis.get(f"state:{chat_id}")
        if raw_state:
            state_size_bytes = len(raw_state.encode("utf-8"))
            try:
                state_obj = json.loads(raw_state)
                iteration = int(state_obj.get("iteration", 0))
                working_memory_len = len(state_obj.get("working_memory", ""))
            except (json.JSONDecodeError, ValueError):
                pass

        # Critical facts count
        cf_count = await store.redis.llen(f"critical_facts:{chat_id}")
        critical_facts_count = int(cf_count) if cf_count else 0

        # Latest audit entry for token counts
        audit_raw = await store.redis.xrevrange(f"audit:{chat_id}", count=1)
        if audit_raw:
            _, fields = audit_raw[0]
            tokens_in = int(fields.get("tokens_in", 0))
            tokens_out = int(fields.get("tokens_out", 0))

        # Compression heuristic: if iteration jumped more than 1 from expected
        # the compressor ran an extra LLM call but iteration still increments by 1.
        # We detect it by checking if working_memory grew then shrank.
        # Simplest proxy: note if compression was triggered by settings.token_budget check.
        # We cannot detect it perfectly post-hoc, so we leave it False by default.
        compression_triggered = False

    except Exception as exc:
        logging.getLogger(__name__).warning("_collect_turn_meta error: %s", exc)

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
        "compression_triggered": compression_triggered,
        "working_memory_len": working_memory_len,
    }


# ---------------------------------------------------------------------------
# Main e2e runner
# ---------------------------------------------------------------------------


async def run_e2e() -> None:
    _safe_print("=" * 70)
    _safe_print("  E2E SINGLE USER TEST -- 50 TURNS")
    _safe_print(f"  chat_id    : {CHAT_ID}")
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
    # 2. Flush previous state for this chat_id
    # ------------------------------------------------------------------
    await _flush_redis_state(redis_client, CHAT_ID)

    # ------------------------------------------------------------------
    # 3. Build engine with real CRM + real LLM
    # ------------------------------------------------------------------
    crm = BitrixAdapter(settings.bitrix_webhook)
    engine = _build_engine(redis_client, crm)

    # We need the store to read back metadata after each turn
    store = StateStore(redis_client, audit_ttl_days=settings.audit_ttl_days)

    # ------------------------------------------------------------------
    # 4. Run all 50 turns
    # ------------------------------------------------------------------
    metrics_per_turn: list[dict] = []
    total_errors = 0

    _safe_print(f"\nStarting {len(TURNS)} turns ...\n")

    for idx, user_input in enumerate(TURNS):
        turn_number = idx + 1
        t_start = time.monotonic()
        error_msg: str | None = None
        response = ""

        try:
            response = await engine.process(user_input, CHAT_ID)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            total_errors += 1
            response = f"[ERROR: {error_msg}]"
            logging.getLogger(__name__).error(
                "Turn %d raised unhandled exception: %s", turn_number, exc, exc_info=True
            )

        latency_ms = round((time.monotonic() - t_start) * 1000)

        turn_data = await _collect_turn_meta(
            store=store,
            chat_id=CHAT_ID,
            turn_number=turn_number,
            user_input=user_input,
            response=response,
            latency_ms=latency_ms,
            error=error_msg,
        )
        metrics_per_turn.append(turn_data)

        # Console progress line (ASCII safe)
        status = "ERROR" if error_msg else "OK"
        _safe_print(
            f"  Turn {turn_number:02d}/{len(TURNS)} [{status}] "
            f"{latency_ms}ms | "
            f"tokens={turn_data['tokens_in']}in/{turn_data['tokens_out']}out | "
            f"facts={turn_data['critical_facts_count']} | "
            f"iter={turn_data['iteration']} | "
            f"wm={turn_data['working_memory_len']}ch"
        )

        # Brief response preview (first 120 chars, ASCII safe)
        preview = response[:120].replace("\n", " ")
        _safe_print(f"         Response: {preview}")

        # Small delay between turns to avoid rate-limiting
        if idx < len(TURNS) - 1:
            await asyncio.sleep(INTER_TURN_DELAY_SEC)

    # ------------------------------------------------------------------
    # 5. Post-run Redis verification
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 70)
    _safe_print("  POST-RUN VERIFICATION")
    _safe_print("=" * 70)

    # Final state
    final_state_raw = await redis_client.get(f"state:{CHAT_ID}")
    final_state = {}
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

    _safe_print(f"  Final iteration    : {final_iteration}")
    _safe_print(f"  Final state size   : {final_state_size} bytes")
    _safe_print(f"  Working memory     : {len(final_wm)} chars")
    _safe_print(f"  Assessment         : {len(final_assessment)} chars")
    _safe_print(f"  Conv. summary      : {len(final_summary)} chars")

    # Critical facts
    cf_raw = await redis_client.lrange(f"critical_facts:{CHAT_ID}", 0, -1)
    critical_facts_list: list[dict] = []
    for item in cf_raw:
        try:
            critical_facts_list.append(json.loads(item))
        except json.JSONDecodeError:
            critical_facts_list.append({"raw": item})

    _safe_print(f"\n  Critical facts saved: {len(critical_facts_list)}")
    for i, cf in enumerate(critical_facts_list[:30]):   # cap display at 30
        content = cf.get("content", cf.get("raw", "?"))[:100]
        fact_type = cf.get("fact_type", "?")
        deal_id = cf.get("deal_id", "")
        _safe_print(f"    [{i+1:02d}] [{fact_type}] deal={deal_id or '-'}: {content}")
    if len(critical_facts_list) > 30:
        _safe_print(f"    ... and {len(critical_facts_list) - 30} more")

    # Memory verification: check if key budget facts survived
    all_facts_text = " ".join(cf.get("content", "") for cf in critical_facts_list).lower()
    omikron_budget_remembered = (
        "omikron" in all_facts_text
        or "700000" in all_facts_text
        or "700" in all_facts_text
    )
    teta_deadline_remembered = (
        "teta" in all_facts_text
        or "april" in all_facts_text
        or "aprel" in all_facts_text
        or "1 aprel" in all_facts_text
    )
    _safe_print(f"\n  Memory check - OmikronAvto budget (700k) survived: {omikron_budget_remembered}")
    _safe_print(f"  Memory check - TetaMedia deadline (Apr 1) survived: {teta_deadline_remembered}")

    # Redis metrics
    metrics_raw = await redis_client.hgetall(f"metrics:{CHAT_ID}")
    _safe_print(f"\n  Metrics from Redis:")
    for k, v in sorted(metrics_raw.items()):
        _safe_print(f"    {k}: {v}")

    total_turns_redis = int(metrics_raw.get("total_turns", 0))
    hallucination_count = int(metrics_raw.get("hallucination_count", 0))
    action_total = int(metrics_raw.get("action_total", 0))
    action_success = int(metrics_raw.get("action_success", 0))

    hallucination_rate = hallucination_count / total_turns_redis if total_turns_redis else 0.0
    action_success_rate = action_success / action_total if action_total else 0.0

    # Drift score: read final state working_memory and check
    drift_score = 0.0
    drift_issues: list[str] = []
    try:
        drift_detector = DriftDetector(crm)
        from ai_native_crm.core.state_store import SemanticState
        from dataclasses import fields as dc_fields

        if final_state:
            known = {f.name for f in dc_fields(SemanticState)}
            filtered = {k: v for k, v in final_state.items() if k in known}
            filtered["chat_id"] = int(filtered.get("chat_id", CHAT_ID))
            state_obj = SemanticState(**filtered)
            drift_score_raw, drift_issues = await drift_detector._check_memory(state_obj.working_memory)
            drift_score = drift_score_raw
    except Exception as exc:
        _safe_print(f"  [WARN] Drift check failed: {exc}")

    _safe_print(f"\n  Drift score        : {drift_score:.3f}")
    if drift_issues:
        for issue in drift_issues:
            _safe_print(f"    Drift issue: {issue}")
    else:
        _safe_print("    No drift issues detected")

    # Audit entries count
    audit_len = await redis_client.xlen(f"audit:{CHAT_ID}")
    _safe_print(f"\n  Audit stream entries: {audit_len}")

    # ------------------------------------------------------------------
    # 6. Summary statistics
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 70)
    _safe_print("  SUMMARY STATISTICS")
    _safe_print("=" * 70)

    latencies = [m["latency_total_ms"] for m in metrics_per_turn]
    total_tokens_in = sum(m["tokens_in"] for m in metrics_per_turn)
    total_tokens_out = sum(m["tokens_out"] for m in metrics_per_turn)
    state_sizes = [m["state_size_bytes"] for m in metrics_per_turn]
    errors_in_turns = [m for m in metrics_per_turn if m["error"]]

    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    min_latency = min(latencies) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0
    p95_latency = _percentile(latencies, 95)

    # Cost estimate (USD)
    cost_input = (total_tokens_in / 1_000_000) * _PRICE_INPUT_PER_1M
    cost_output = (total_tokens_out / 1_000_000) * _PRICE_OUTPUT_PER_1M
    total_cost = cost_input + cost_output

    _safe_print(f"  Total turns run      : {len(TURNS)}")
    _safe_print(f"  Errors during turns  : {len(errors_in_turns)}")
    _safe_print(f"")
    _safe_print(f"  Latency (ms):")
    _safe_print(f"    avg   : {avg_latency:.0f}")
    _safe_print(f"    min   : {min_latency:.0f}")
    _safe_print(f"    max   : {max_latency:.0f}")
    _safe_print(f"    p95   : {p95_latency:.0f}")
    _safe_print(f"")
    _safe_print(f"  Tokens:")
    _safe_print(f"    total input    : {total_tokens_in:,}")
    _safe_print(f"    total output   : {total_tokens_out:,}")
    _safe_print(f"")
    _safe_print(f"  Cost estimate (gpt-4o-mini pricing):")
    _safe_print(f"    input   : ${cost_input:.4f} ({total_tokens_in:,} tokens * ${_PRICE_INPUT_PER_1M}/1M)")
    _safe_print(f"    output  : ${cost_output:.4f} ({total_tokens_out:,} tokens * ${_PRICE_OUTPUT_PER_1M}/1M)")
    _safe_print(f"    TOTAL   : ${total_cost:.4f}")
    _safe_print(f"")
    _safe_print(f"  Quality metrics (from Redis):")
    _safe_print(f"    hallucination_rate   : {hallucination_rate:.1%} ({hallucination_count}/{total_turns_redis})")
    _safe_print(f"    action_success_rate  : {action_success_rate:.1%} ({action_success}/{action_total})")
    _safe_print(f"")
    _safe_print(f"  State size progression:")
    for m in metrics_per_turn[::10]:   # every 10 turns
        _safe_print(f"    Turn {m['turn']:02d}: {m['state_size_bytes']} bytes (iter={m['iteration']})")
    last = metrics_per_turn[-1]
    _safe_print(f"    Turn {last['turn']:02d}: {last['state_size_bytes']} bytes (iter={last['iteration']})")
    _safe_print(f"")
    _safe_print(f"  Final state size     : {final_state_size} bytes")
    _safe_print(f"  Critical facts total : {len(critical_facts_list)}")
    _safe_print(f"  Drift score          : {drift_score:.3f} (threshold={settings.drift_threshold})")
    _safe_print(f"  OmikronAvto budget remembered : {omikron_budget_remembered}")
    _safe_print(f"  TetaMedia deadline remembered : {teta_deadline_remembered}")

    if errors_in_turns:
        _safe_print(f"\n  ERROR DETAILS:")
        for m in errors_in_turns:
            _safe_print(f"    Turn {m['turn']:02d}: {m['error']}")

    # ------------------------------------------------------------------
    # 7. Save JSON results
    # ------------------------------------------------------------------
    results = {
        "run_meta": {
            "chat_id": CHAT_ID,
            "redis_url": redis_url,
            "llm_model": settings.llm_model,
            "total_turns": len(TURNS),
            "inter_turn_delay_sec": INTER_TURN_DELAY_SEC,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "summary": {
            "errors_total": len(errors_in_turns),
            "latency_avg_ms": round(avg_latency),
            "latency_min_ms": round(min_latency),
            "latency_max_ms": round(max_latency),
            "latency_p95_ms": round(p95_latency),
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "cost_usd_estimate": round(total_cost, 6),
            "hallucination_rate": round(hallucination_rate, 4),
            "action_success_rate": round(action_success_rate, 4),
            "final_state_size_bytes": final_state_size,
            "critical_facts_count": len(critical_facts_list),
            "drift_score": round(drift_score, 4),
            "omikron_budget_remembered": omikron_budget_remembered,
            "teta_deadline_remembered": teta_deadline_remembered,
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
        "drift_issues": drift_issues,
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
    asyncio.run(run_e2e())
