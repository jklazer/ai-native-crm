"""
Автоматическое демо AI-Native CRM Agent.
Демонстрирует все 10 возможностей за 2 минуты.

Запуск: python demo.py
Требования: Redis на localhost:6379, OpenAI API key в .env
"""
import asyncio
import json
import os
import time
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"

# Force stdout/stderr to UTF-8 on Windows (cp1251 terminals can't handle Cyrillic
# responses from the LLM that contain characters outside the cp1251 range, e.g. ₽).
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def header(text: str) -> None:
    """Print a formatted section header."""
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)
    print()


def step(num: int, title: str) -> None:
    """Print a step header."""
    print(f"\n{'-' * 50}")
    print(f"  Step {num}/10: {title}")
    print(f"{'-' * 50}\n")


def show_response(question: str, answer: str) -> None:
    """Print Q&A pair."""
    print(f"  Manager: {question}")
    print(f"  Agent:   {answer[:200]}")
    print()


async def demo():
    # --- Setup ---
    from ai_native_crm.config import settings
    settings.crm_adapter = "mock"  # Use mock adapter, no real CRM needed

    import redis.asyncio as aioredis
    from ai_native_crm.adapters.mock import MockAdapter
    from ai_native_crm.core.state_store import StateStore
    from ai_native_crm.services.llm_client import LLMClient
    from ai_native_crm.core.response_validator import ResponseValidator
    from ai_native_crm.core.action_router import ActionRouter
    from ai_native_crm.core.compressor import StateCompressor
    from ai_native_crm.core.drift_detector import DriftDetector
    from ai_native_crm.services.pii_anonymizer import PIIAnonymizer
    from ai_native_crm.services.lock import DistributedLock
    from ai_native_crm.services.metrics import MetricsService
    from ai_native_crm.core.engine import AgentEngine

    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    adapter = MockAdapter()
    store = StateStore(r, audit_ttl_days=30)
    llm = LLMClient()
    validator = ResponseValidator(adapter)
    action_router = ActionRouter(adapter, None, store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(adapter)
    pii = PIIAnonymizer(r)
    lock = DistributedLock(r)
    metrics = MetricsService(store)
    engine = AgentEngine(
        state_store=store, crm=adapter, llm=llm, validator=validator,
        action_router=action_router, compressor=compressor, drift=drift,
        anonymizer=pii, lock=lock, metrics=metrics,
    )

    CHAT_ID = 99999
    await r.delete(f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
                   f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}")

    header("AI-Native CRM Agent Demo")
    print("  Architecture: Zero-DB (Redis-only, no PostgreSQL)")
    print("  CRM: MockAdapter (5 deals, 3 contacts)")
    print("  LLM: GPT-4o-mini + Claude Haiku fallback")
    print()

    t_total = time.time()

    # Step 1: Load deals from CRM
    step(1, "Loading deals from CRM (source of truth)")
    resp = await engine.process("Покажи все текущие сделки", CHAT_ID)
    show_response("Покажи все текущие сделки", resp)

    # Step 2: Contextual question about a deal
    step(2, "Contextual Q&A about a specific deal")
    resp = await engine.process("Какая сумма у самой крупной сделки?", CHAT_ID)
    show_response("Какая сумма у самой крупной сделки?", resp)

    # Step 3: Action — update deal stage
    step(3, "CRM Action — update deal stage")
    resp = await engine.process("Переведи сделку d1 в стадию переговоров", CHAT_ID)
    show_response("Переведи сделку d1 в стадию переговоров", resp)

    # Step 4: Critical fact extraction
    step(4, "Critical Fact — budget limit")
    resp = await engine.process("Клиент по сделке d2 сказал: бюджет максимум 300 тысяч, выше не пойдут. Зафиксируй.", CHAT_ID)
    show_response("Клиент сказал: бюджет максимум 300к", resp)
    facts = await store.get_critical_facts(CHAT_ID)
    print(f"  Critical facts stored: {len(facts)}")
    for f in facts:
        print(f"    [{f.fact_type}] {f.content}")

    # Step 5: State compression
    step(5, "State Compression (force via low token budget)")
    original_budget = settings.token_budget
    settings.token_budget = 30  # Force compression
    resp = await engine.process("Детальный анализ всех сделок с рисками и рекомендациями", CHAT_ID)
    show_response("Детальный анализ всех сделок", resp)
    settings.token_budget = original_budget
    state = await store.load(CHAT_ID)
    print(f"  State after compression: {len(state.working_memory)} chars WM")
    print(f"  conversation_summary: {state.conversation_summary[:100]}...")

    # Step 6: Memory survival after compression
    step(6, "Memory Survival — recall budget after compression")
    resp = await engine.process("Какой бюджет был у клиента по второй сделке?", CHAT_ID)
    show_response("Какой бюджет был у клиента?", resp)
    budget_mentioned = "300" in resp
    print(f"  Budget 300k recalled: {'YES' if budget_mentioned else 'NO (check critical facts)'}")

    # Step 7: Anti-hallucination
    step(7, "Anti-Hallucination — fake deal ID")
    resp = await engine.process("Покажи сделку d999 — какая там сумма?", CHAT_ID)
    show_response("Покажи сделку d999", resp)
    hallucinated = "d999" in resp and any(c.isdigit() for c in resp.split("d999")[-1][:20])
    print(f"  Hallucination blocked: {'YES (safe)' if not hallucinated else 'NO (DANGER!)'}")

    # Step 8: PII anonymization
    step(8, "PII Anonymization (152-FZ)")
    test_text = "Позвони Козлову Дмитрию Петровичу по +79161234567"
    anon = await pii.anonymize(test_text, str(CHAT_ID))
    deanon = await pii.deanonymize(anon, str(CHAT_ID))
    print(f"  Original:     {test_text}")
    print(f"  Anonymized:   {anon}")
    print(f"  Deanonymized: {deanon}")
    print(f"  Round-trip OK: {deanon == test_text}")

    # Step 9: Metrics
    step(9, "Quality Metrics")
    m = await store.get_metrics(CHAT_ID)
    print(f"  Total turns:        {m.get('total_turns', 0)}")
    print(f"  Hallucinations:     {m.get('hallucination_total', 0)}")
    print(f"  Action success:     {m.get('action_success', 0)}/{m.get('action_total', 0)}")

    # Step 10: Drift detection
    step(10, "Drift Detection — CRM sync check")
    state = await store.load(CHAT_ID)
    drift_score = await drift.check(state)
    print(f"  Drift score: {drift_score:.2f} (0.0 = perfect sync, 1.0 = total drift)")
    print(f"  Status: {'OK' if drift_score < 0.4 else 'WARNING — state drifted from CRM!'}")

    # Final summary
    elapsed = time.time() - t_total
    header(f"Demo Complete ({elapsed:.0f}s)")
    print("  10/10 capabilities demonstrated")
    print(f"  Total turns processed: {int(m.get('total_turns', 0))}")
    print(f"  Zero SQL queries executed")
    print(f"  All data persisted in Redis only")
    print()
    print("  GitHub: https://github.com/YOUR/ai-native-crm")
    print("  License: MIT")
    print()

    await r.aclose()


if __name__ == "__main__":
    # Suppress the "Event loop is closed" noise on Python 3.9 / Windows ProactorEventLoop.
    # The warning fires during GC when asyncio transport __del__ tries to schedule a
    # callback into the already-closed loop — harmless, but ugly in terminal recordings.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(demo())
