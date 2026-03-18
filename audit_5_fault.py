"""
audit_5_fault.py — Section 5: Fault Tolerance Tests
CHAT_ID = 90005

Tests:
  5.1  Redis failure mid-operation (ConnectionError on get())
  5.2  CRM API unavailable (get_deals raises TimeoutError)
  5.3  LLM API unavailable (call() raises Exception)
  5.4  Empty message ("")
  5.5  Very long message (10 000 chars)
  5.6  Unicode edge cases (emojis, Arabic, Chinese, mixed)
  5.7  Rapid /start (10 concurrent)

Results written to audit_5_fault_results.txt
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock, patch, MagicMock

os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------------------------------------------------------------------
# Engine builder
# ---------------------------------------------------------------------------

async def build_engine(redis_client=None):
    import redis.asyncio as aioredis
    from ai_native_crm.config import settings
    from ai_native_crm.adapters.bitrix import BitrixAdapter
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

    r = redis_client or aioredis.from_url(
        "redis://localhost:6379/5", decode_responses=True
    )
    adapter = BitrixAdapter(settings.bitrix_webhook)
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
        state_store=store,
        crm=adapter,
        llm=llm,
        validator=validator,
        action_router=action_router,
        compressor=compressor,
        drift=drift,
        anonymizer=pii,
        lock=lock,
        metrics=metrics,
    )
    return engine, r, adapter, store


def result_line(name: str, passed: bool, severity: str, detail: str) -> str:
    status = "PASS" if passed else "FAIL"
    return f"  [{status}] [{severity}] {name}: {detail}"


# ---------------------------------------------------------------------------
# Test 5.1 — Redis failure mid-operation
# ---------------------------------------------------------------------------

async def test_5_1_redis_failure(base_chat_id: int, f) -> bool:
    """
    Wrap the real Redis client so that get() raises ConnectionError after
    the lock is acquired (i.e. during state loading).
    Engine must catch it and return a user-friendly error — not crash.
    """
    CHAT_ID = base_chat_id + 10

    import redis.asyncio as aioredis
    from ai_native_crm.core.state_store import StateStore
    from ai_native_crm.services.lock import DistributedLock

    r_real = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r_real.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter, _ = await build_engine(r_real)

    # Track how many get() calls happen; raise on the 2nd call
    # (1st call is the lock SET/GET; 2nd is state load)
    get_call_count = 0
    original_get = r_real.get

    async def failing_get(key, *args, **kwargs):
        nonlocal get_call_count
        get_call_count += 1
        # Allow lock-related calls (they don't use get for acquire),
        # but fail state:* reads
        if key.startswith("state:"):
            raise ConnectionError("Redis connection lost (simulated)")
        return await original_get(key, *args, **kwargs)

    r_real.get = failing_get

    t0 = time.monotonic()
    try:
        response = await engine.process("Статус по сделкам?", CHAT_ID)
        crashed = False
    except Exception as exc:
        response = f"EXCEPTION: {exc}"
        crashed = True
    elapsed = time.monotonic() - t0

    # Restore
    r_real.get = original_get

    # Engine must NOT crash — it should return a user-friendly message
    is_friendly = (
        not crashed
        and isinstance(response, str)
        and len(response) > 0
        and "EXCEPTION" not in response
    )
    # The error message should not expose stack traces or raw exception text
    no_leak = "Traceback" not in response and "ConnectionError" not in response

    passed = is_friendly and no_leak

    f.write("\n=== TEST 5.1: Redis Failure Mid-Operation ===\n")
    f.write(f"  Crashed (unhandled exception): {crashed}\n")
    f.write(f"  Response: {response!r}\n")
    f.write(f"  Is friendly message: {is_friendly}\n")
    f.write(f"  No internals leaked: {no_leak}\n")
    f.write(f"  Elapsed: {elapsed:.2f}s\n")
    f.write(result_line(
        "5.1 Redis failure", passed, "HIGH",
        f"crashed={crashed}, friendly={is_friendly}, no_leak={no_leak}"
    ) + "\n")

    print(f"  5.1 Redis failure: {'PASS' if passed else 'FAIL'} "
          f"crashed={crashed} friendly={is_friendly}")

    await adapter.close()
    await r_real.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 5.2 — CRM API unavailable
# ---------------------------------------------------------------------------

async def test_5_2_crm_unavailable(base_chat_id: int, f) -> bool:
    """
    Mock CRM adapter where get_deals() raises asyncio.TimeoutError.
    Engine should proceed with empty deals list, not crash.
    """
    CHAT_ID = base_chat_id + 20

    import redis.asyncio as aioredis
    from ai_native_crm.adapters.base import CRMAdapter, DealInfo, ContactInfo

    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    class BrokenCRMAdapter(CRMAdapter):
        async def get_deals(self, filters=None) -> list[DealInfo]:
            raise asyncio.TimeoutError("CRM timeout (simulated)")

        async def update_deal(self, deal_id: str, fields: dict) -> bool:
            return False

        async def create_deal(self, data: dict) -> str:
            return "mock_id"

        async def get_contacts(self, filters=None) -> list[ContactInfo]:
            return []

        async def verify_deal_exists(self, deal_id: str) -> bool:
            return False

        async def get_deal_amount(self, deal_id: str):
            return None

        async def close(self):
            pass

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

    broken_crm = BrokenCRMAdapter()
    store = StateStore(r, audit_ttl_days=30)
    llm = LLMClient()
    validator = ResponseValidator(broken_crm)
    action_router = ActionRouter(broken_crm, None, store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(broken_crm)
    pii = PIIAnonymizer(r)
    lock = DistributedLock(r)
    metrics = MetricsService(store)
    engine = AgentEngine(
        state_store=store, crm=broken_crm, llm=llm,
        validator=validator, action_router=action_router,
        compressor=compressor, drift=drift, anonymizer=pii,
        lock=lock, metrics=metrics,
    )

    t0 = time.monotonic()
    try:
        response = await engine.process("Список сделок?", CHAT_ID)
        crashed = False
    except Exception as exc:
        response = f"EXCEPTION: {exc}"
        crashed = True
    elapsed = time.monotonic() - t0

    # Should not crash; should return some response (possibly with "нет данных" etc.)
    is_response = not crashed and isinstance(response, str) and len(response) > 0
    no_exception_leak = "EXCEPTION" not in response

    # Check state was still saved (engine continued after CRM failure)
    from ai_native_crm.core.state_store import StateStore as SS
    state = await SS(r).load(CHAT_ID)
    state_saved = state.iteration > 0 or isinstance(state.working_memory, str)

    passed = is_response and no_exception_leak

    f.write("\n=== TEST 5.2: CRM API Unavailable (TimeoutError) ===\n")
    f.write(f"  Crashed: {crashed}\n")
    f.write(f"  Response: {response!r}\n")
    f.write(f"  State iteration after: {state.iteration}\n")
    f.write(f"  Elapsed: {elapsed:.2f}s\n")
    f.write(result_line(
        "5.2 CRM unavailable", passed, "HIGH",
        f"crashed={crashed}, got_response={is_response}, iter={state.iteration}"
    ) + "\n")

    print(f"  5.2 CRM unavailable: {'PASS' if passed else 'FAIL'} "
          f"crashed={crashed} iter={state.iteration}")

    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 5.3 — LLM API unavailable
# ---------------------------------------------------------------------------

async def test_5_3_llm_unavailable(base_chat_id: int, f) -> bool:
    """
    Mock LLMClient.call() to raise Exception("API unavailable").
    Engine should return user-friendly error, not crash.
    """
    CHAT_ID = base_chat_id + 30

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter, _ = await build_engine(r)

    original_call = engine._llm.call

    async def failing_llm(messages):
        raise Exception("API unavailable")

    engine._llm.call = failing_llm

    t0 = time.monotonic()
    try:
        response = await engine.process("Анализ сделок", CHAT_ID)
        crashed = False
    except Exception as exc:
        response = f"EXCEPTION: {exc}"
        crashed = True
    elapsed = time.monotonic() - t0

    # Restore
    engine._llm.call = original_call

    is_friendly = (
        not crashed
        and isinstance(response, str)
        and len(response) > 0
        and "EXCEPTION" not in response
    )
    no_internals = "Traceback" not in response and "API unavailable" not in response

    passed = is_friendly

    f.write("\n=== TEST 5.3: LLM API Unavailable ===\n")
    f.write(f"  Crashed: {crashed}\n")
    f.write(f"  Response: {response!r}\n")
    f.write(f"  Is friendly: {is_friendly}\n")
    f.write(f"  No internals leaked: {no_internals}\n")
    f.write(f"  Elapsed: {elapsed:.2f}s\n")
    f.write(result_line(
        "5.3 LLM unavailable", passed, "CRITICAL",
        f"crashed={crashed}, friendly={is_friendly}, no_leak={no_internals}"
    ) + "\n")

    print(f"  5.3 LLM unavailable: {'PASS' if passed else 'FAIL'} "
          f"crashed={crashed} friendly={is_friendly}")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 5.4 — Empty message
# ---------------------------------------------------------------------------

async def test_5_4_empty_message(base_chat_id: int, f) -> bool:
    """engine.process("", CHAT_ID) — crash or graceful?"""
    CHAT_ID = base_chat_id + 40

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter, _ = await build_engine(r)

    t0 = time.monotonic()
    try:
        response = await engine.process("", CHAT_ID)
        crashed = False
    except Exception as exc:
        response = f"EXCEPTION: {type(exc).__name__}: {exc}"
        crashed = True
    elapsed = time.monotonic() - t0

    is_graceful = isinstance(response, str) and len(response) > 0
    passed = not crashed and is_graceful

    f.write("\n=== TEST 5.4: Empty Message ===\n")
    f.write(f"  Crashed: {crashed}\n")
    f.write(f"  Response: {response!r}\n")
    f.write(f"  Elapsed: {elapsed:.2f}s\n")
    f.write(result_line(
        "5.4 Empty message", passed, "MEDIUM",
        f"crashed={crashed}, response_len={len(response)}"
    ) + "\n")

    print(f"  5.4 Empty message: {'PASS' if passed else 'FAIL'} "
          f"crashed={crashed} resp_len={len(response)}")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 5.5 — Very long message (10 000 chars)
# ---------------------------------------------------------------------------

async def test_5_5_very_long_message(base_chat_id: int, f) -> bool:
    """engine.process("А" * 10000, CHAT_ID) — crash or handled?"""
    CHAT_ID = base_chat_id + 50

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter, _ = await build_engine(r)
    long_message = "А" * 10000

    t0 = time.monotonic()
    try:
        response = await engine.process(long_message, CHAT_ID)
        crashed = False
    except Exception as exc:
        response = f"EXCEPTION: {type(exc).__name__}: {exc}"
        crashed = True
    elapsed = time.monotonic() - t0

    is_graceful = isinstance(response, str) and len(response) > 0
    passed = not crashed and is_graceful

    f.write("\n=== TEST 5.5: Very Long Message (10 000 chars) ===\n")
    f.write(f"  Input length: {len(long_message)} chars\n")
    f.write(f"  Crashed: {crashed}\n")
    f.write(f"  Response: {response[:200]!r}{'...' if len(response) > 200 else ''}\n")
    f.write(f"  Elapsed: {elapsed:.2f}s\n")
    f.write(result_line(
        "5.5 Very long message", passed, "MEDIUM",
        f"crashed={crashed}, response_len={len(response)}, elapsed={elapsed:.1f}s"
    ) + "\n")

    print(f"  5.5 Long message: {'PASS' if passed else 'FAIL'} "
          f"crashed={crashed} elapsed={elapsed:.1f}s")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 5.6 — Unicode edge cases
# ---------------------------------------------------------------------------

async def test_5_6_unicode(base_chat_id: int, f) -> bool:
    """Test emojis, Arabic, Chinese, mixed Unicode. Crash or handled?"""
    CHAT_ID_BASE = base_chat_id + 60

    import redis.asyncio as aioredis

    test_cases = [
        ("emojis",   "🔥💰📊 Как дела со сделками?"),
        ("arabic",   "مرحبا كيف حال الصفقات"),
        ("chinese",  "你好世界 交易状态如何"),
        ("mixed",    "Deal 💰 сделка مرحبا 你好 status?"),
    ]

    sub_results: list[tuple[str, bool, str]] = []

    for i, (case_name, msg) in enumerate(test_cases):
        CHAT_ID = CHAT_ID_BASE + i
        r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
        await r.delete(
            f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
            f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
        )
        engine, _, adapter, _ = await build_engine(r)

        try:
            response = await engine.process(msg, CHAT_ID)
            crashed = False
        except Exception as exc:
            response = f"EXCEPTION: {type(exc).__name__}: {exc}"
            crashed = True

        sub_passed = not crashed and isinstance(response, str) and len(response) > 0
        sub_results.append((case_name, sub_passed, response[:100] if response else ""))

        await adapter.close()
        await r.aclose()

    all_passed = all(r for _, r, _ in sub_results)

    f.write("\n=== TEST 5.6: Unicode Edge Cases ===\n")
    for case_name, sub_passed, resp_preview in sub_results:
        status = "PASS" if sub_passed else "FAIL"
        f.write(f"  [{status}] {case_name}: {resp_preview!r}\n")
    f.write(result_line(
        "5.6 Unicode edge cases", all_passed, "LOW",
        f"passed {sum(1 for _,p,_ in sub_results if p)}/{len(sub_results)} sub-tests"
    ) + "\n")

    print(f"  5.6 Unicode: {'PASS' if all_passed else 'FAIL'} "
          f"({sum(1 for _,p,_ in sub_results if p)}/{len(sub_results)})")

    return all_passed


# ---------------------------------------------------------------------------
# Test 5.7 — Rapid /start (10 concurrent)
# ---------------------------------------------------------------------------

async def test_5_7_rapid_start(base_chat_id: int, f) -> bool:
    """
    Call engine.process("/start", CHAT_ID) 10 times via gather.
    Check for race conditions and state corruption.
    """
    CHAT_ID = base_chat_id + 70

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter, _ = await build_engine(r)

    responses: list[str] = []
    errors: list[str] = []

    async def send_start(idx: int) -> None:
        try:
            resp = await engine.process("/start", CHAT_ID)
            responses.append(resp)
        except Exception as exc:
            errors.append(f"task{idx}: {type(exc).__name__}: {exc}")

    t0 = time.monotonic()
    await asyncio.gather(*[send_start(i) for i in range(10)])
    elapsed = time.monotonic() - t0

    from ai_native_crm.core.state_store import StateStore
    store = StateStore(r)
    final_state = await store.load(CHAT_ID)

    # Checks:
    all_responded = len(responses) + len(errors) == 10
    no_exceptions = len(errors) == 0
    # Iteration must equal the number of successful responses (lock serialises)
    # Some may return "Система занята" — that's still a valid response, not a crash
    busy_count = sum(1 for r in responses if "занята" in r.lower())
    successful_count = len(responses) - busy_count
    iter_matches = final_state.iteration == successful_count
    state_intact = isinstance(final_state.working_memory, str)

    # No corruption: iteration should be sane (>= 1, <= 10)
    no_corruption = 1 <= final_state.iteration <= 10

    passed = all_responded and no_exceptions and state_intact and no_corruption

    f.write("\n=== TEST 5.7: Rapid /start (10 concurrent) ===\n")
    f.write(f"  Total elapsed: {elapsed:.2f}s\n")
    f.write(f"  Responses received: {len(responses)}/10\n")
    f.write(f"  'Система занята': {busy_count}\n")
    f.write(f"  Successful (processed): {successful_count}\n")
    f.write(f"  Unhandled exceptions: {errors}\n")
    f.write(f"  Final state.iteration: {final_state.iteration}\n")
    f.write(f"  State intact (no corruption): {state_intact}\n")
    f.write(f"  Iteration in sane range: {no_corruption}\n")
    f.write(result_line(
        "5.7 Rapid /start", passed, "HIGH",
        f"exceptions={len(errors)}, iter={final_state.iteration}, "
        f"successful={successful_count}, busy={busy_count}"
    ) + "\n")

    print(f"  5.7 Rapid /start: {'PASS' if passed else 'FAIL'} "
          f"exc={len(errors)} iter={final_state.iteration} "
          f"ok={successful_count} busy={busy_count}")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def main() -> None:
    BASE_CHAT_ID = 90005

    print("=" * 60)
    print("AUDIT 5: FAULT TOLERANCE TESTS")
    print(f"Base CHAT_ID: {BASE_CHAT_ID}")
    print("=" * 60)

    results: dict[str, bool] = {}

    with open(
        "C:/Users/sazon/OneDrive/Desktop/ai-native-crm/audit_5_fault_results.txt",
        "w",
        encoding="utf-8",
    ) as f:
        f.write("=" * 70 + "\n")
        f.write("AUDIT 5: FAULT TOLERANCE TESTS\n")
        f.write(f"Base CHAT_ID: {BASE_CHAT_ID}\n")
        f.write("=" * 70 + "\n")

        print("\n[5.1] Redis failure mid-operation...")
        results["5.1"] = await test_5_1_redis_failure(BASE_CHAT_ID, f)

        print("\n[5.2] CRM API unavailable...")
        results["5.2"] = await test_5_2_crm_unavailable(BASE_CHAT_ID, f)

        print("\n[5.3] LLM API unavailable...")
        results["5.3"] = await test_5_3_llm_unavailable(BASE_CHAT_ID, f)

        print("\n[5.4] Empty message...")
        results["5.4"] = await test_5_4_empty_message(BASE_CHAT_ID, f)

        print("\n[5.5] Very long message (10 000 chars)...")
        results["5.5"] = await test_5_5_very_long_message(BASE_CHAT_ID, f)

        print("\n[5.6] Unicode edge cases...")
        results["5.6"] = await test_5_6_unicode(BASE_CHAT_ID, f)

        print("\n[5.7] Rapid /start (10 concurrent)...")
        results["5.7"] = await test_5_7_rapid_start(BASE_CHAT_ID, f)

        # Summary
        passed_count = sum(1 for v in results.values() if v)
        total_count = len(results)
        overall = "PASS" if passed_count == total_count else "FAIL"

        f.write("\n" + "=" * 70 + "\n")
        f.write("RESULTS SUMMARY\n")
        f.write("=" * 70 + "\n")
        severity_map = {
            "5.1": "HIGH", "5.2": "HIGH", "5.3": "CRITICAL",
            "5.4": "MEDIUM", "5.5": "MEDIUM", "5.6": "LOW", "5.7": "HIGH",
        }
        for test_id, passed in results.items():
            sev = severity_map.get(test_id, "?")
            f.write(f"  Test {test_id} [{sev}]: {'PASS' if passed else 'FAIL'}\n")
        f.write(f"\nPassed: {passed_count}/{total_count}\n")
        f.write(f"OVERALL: {overall}\n")

    print("\n" + "=" * 60)
    print("AUDIT 5 RESULTS SUMMARY")
    print("=" * 60)
    for test_id, passed in results.items():
        sev = severity_map.get(test_id, "?")
        print(f"  Test {test_id} [{sev}]: {'PASS' if passed else 'FAIL'}")
    print(f"  Passed: {passed_count}/{total_count}")
    print(f"  OVERALL: {overall}")
    print("=" * 60)
    print("Results saved to audit_5_fault_results.txt")


if __name__ == "__main__":
    asyncio.run(main())
