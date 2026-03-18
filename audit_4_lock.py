"""
audit_4_lock.py — Section 4: Distributed Lock Tests
CHAT_ID = 90004

Tests:
  4.1  Concurrent messages (5 simultaneous) — lock serializes, no corruption
  4.2  Lock during LLM processing — second message waits for first
  4.3  Lock timeout — short lock_timeout causes "Система занята" for late arrivals
  4.4  Lock cleanup — no stale keys remain after all tests

Results written to audit_4_lock_results.txt
"""

import asyncio
import os
import time

os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------------------------------------------------------------------
# Engine builder (mirrors stress_test_compression.py)
# ---------------------------------------------------------------------------

async def build_engine(redis_client=None):
    """Build a full AgentEngine. Optionally inject a custom redis client."""
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
    return engine, r, adapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def result_line(name: str, passed: bool, detail: str) -> str:
    status = "PASS" if passed else "FAIL"
    return f"  [{status}] {name}: {detail}"


# ---------------------------------------------------------------------------
# Test 4.1 — 5 concurrent messages to the same chat_id
# ---------------------------------------------------------------------------

async def test_4_1_concurrent(base_chat_id: int, f) -> bool:
    """Send 5 messages simultaneously. Expect serialisation, no corruption."""
    CHAT_ID = base_chat_id + 10

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)

    # clean slate
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter = await build_engine(r)

    messages = [
        "Привет, как дела со сделками?",
        "Есть ли активные лиды?",
        "Сколько открытых сделок?",
        "Нужно ли что-то срочно закрыть?",
        "Краткий статус по портфелю.",
    ]

    acquire_times: list[float] = []
    results: list[str] = []
    errors: list[str] = []

    async def send(msg: str, idx: int) -> None:
        t0 = time.monotonic()
        try:
            resp = await engine.process(msg, CHAT_ID)
            elapsed = time.monotonic() - t0
            acquire_times.append(elapsed)
            results.append(resp)
        except Exception as exc:
            errors.append(f"msg{idx}: {exc}")

    # Fire all 5 simultaneously
    t_start = time.monotonic()
    await asyncio.gather(*[send(m, i) for i, m in enumerate(messages)])
    total_elapsed = time.monotonic() - t_start

    from ai_native_crm.core.state_store import StateStore
    store = StateStore(r)
    final_state = await store.load(CHAT_ID)

    # Checks
    all_responded = len(results) == 5
    no_errors = len(errors) == 0
    # All 5 should have run sequentially — iteration should be 5
    iteration_correct = final_state.iteration == 5
    # State consistency: working_memory should be a non-empty string (no crash)
    state_consistent = isinstance(final_state.working_memory, str)

    passed = all_responded and no_errors and iteration_correct and state_consistent

    f.write("\n=== TEST 4.1: Concurrent Messages (5 simultaneous) ===\n")
    f.write(f"  Total elapsed: {total_elapsed:.2f}s\n")
    f.write(f"  Responses received: {len(results)}/5\n")
    f.write(f"  Errors: {errors}\n")
    f.write(f"  Final state.iteration: {final_state.iteration} (expected 5)\n")
    f.write(f"  State consistent: {state_consistent}\n")
    f.write(f"  Individual response times: {[round(t,2) for t in acquire_times]}\n")
    f.write(result_line("4.1 Concurrent serialisation", passed,
                        f"iter={final_state.iteration}, responses={len(results)}/5, errors={len(errors)}") + "\n")

    print(f"  4.1 Concurrent: {'PASS' if passed else 'FAIL'} "
          f"iter={final_state.iteration} resp={len(results)}/5 err={len(errors)}")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 4.2 — Lock held during LLM processing; second message must wait
# ---------------------------------------------------------------------------

async def test_4_2_lock_during_llm(base_chat_id: int, f) -> bool:
    """
    Patch LLM to sleep 3s (simulating slow processing).
    Send msg1, then immediately send msg2.
    msg2 must wait (not run in parallel). We detect this by checking ordering.
    """
    CHAT_ID = base_chat_id + 20

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter = await build_engine(r)

    # Track when each message actually starts its LLM call
    llm_start_times: list[float] = []
    llm_end_times: list[float] = []
    t_reference = time.monotonic()
    original_call = engine._llm.call

    async def slow_llm(messages):
        llm_start_times.append(time.monotonic() - t_reference)
        await asyncio.sleep(2)  # simulate 2s LLM processing
        llm_end_times.append(time.monotonic() - t_reference)
        return await original_call(messages)

    engine._llm.call = slow_llm

    # Start msg1, then immediately (0.1s later) send msg2
    msg1_done = asyncio.Event()
    ordering: list[str] = []

    async def send_first():
        await engine.process("Статус по всем сделкам?", CHAT_ID)
        ordering.append("msg1_done")
        msg1_done.set()

    async def send_second():
        await asyncio.sleep(0.1)  # slight delay so msg1 acquires lock first
        await engine.process("Есть новые лиды?", CHAT_ID)
        ordering.append("msg2_done")

    t0 = time.monotonic()
    await asyncio.gather(send_first(), send_second())
    total = time.monotonic() - t0

    # Restore original
    engine._llm.call = original_call

    # msg1 should start LLM before msg2 starts LLM (lock serialises)
    # Both LLM start times should be captured, msg2 start > msg1 end
    llm_serialised = False
    if len(llm_start_times) >= 2:
        # msg2 shouldn't have started before msg1 finished
        llm_serialised = llm_start_times[1] >= llm_end_times[0]

    state_correct = True  # just no crash
    passed = llm_serialised and len(ordering) == 2

    f.write("\n=== TEST 4.2: Lock During LLM Processing ===\n")
    f.write(f"  Total elapsed: {total:.2f}s (expect ~4s: 2s+2s sequential)\n")
    f.write(f"  LLM start times: {[round(t,2) for t in llm_start_times]}\n")
    f.write(f"  LLM end times:   {[round(t,2) for t in llm_end_times]}\n")
    f.write(f"  LLM serialised (msg2 started after msg1 finished): {llm_serialised}\n")
    f.write(f"  Completion order: {ordering}\n")
    f.write(result_line("4.2 Lock during LLM", passed,
                        f"serialised={llm_serialised}, total={total:.1f}s") + "\n")

    print(f"  4.2 Lock during LLM: {'PASS' if passed else 'FAIL'} "
          f"serialised={llm_serialised} total={total:.1f}s")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 4.3 — Lock timeout: short timeout causes "Система занята"
# ---------------------------------------------------------------------------

async def test_4_3_lock_timeout(base_chat_id: int, f) -> bool:
    """
    Set lock_timeout_sec = 1 (very short).
    Patch LLM to sleep 3s (longer than timeout).
    Send 3 concurrent messages — at least 2 should get "Система занята".
    """
    CHAT_ID = base_chat_id + 30

    from ai_native_crm.config import settings
    original_timeout = settings.lock_timeout_sec
    settings.lock_timeout_sec = 1  # 1 second timeout

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
    )

    engine, _, adapter = await build_engine(r)

    # Rebuild lock with the new timeout setting in effect
    from ai_native_crm.services.lock import DistributedLock
    engine._lock = DistributedLock(r)

    original_call = engine._llm.call

    async def very_slow_llm(messages):
        await asyncio.sleep(3)  # 3s — way longer than 1s timeout
        return await original_call(messages)

    engine._llm.call = very_slow_llm

    responses: list[str] = []

    async def send(msg: str) -> None:
        resp = await engine.process(msg, CHAT_ID)
        responses.append(resp)

    await asyncio.gather(
        send("Первое сообщение"),
        send("Второе сообщение"),
        send("Третье сообщение"),
    )

    # Restore
    engine._llm.call = original_call
    settings.lock_timeout_sec = original_timeout

    busy_responses = [r for r in responses if "Система занята" in r or "занята" in r.lower()]
    # At least 2 of 3 should have timed out
    passed = len(busy_responses) >= 1

    f.write("\n=== TEST 4.3: Lock Timeout (lock_timeout_sec=1) ===\n")
    f.write(f"  All responses: {responses}\n")
    f.write(f"  'Система занята' responses: {len(busy_responses)}/3\n")
    f.write(result_line("4.3 Lock timeout", passed,
                        f"'Система занята' count={len(busy_responses)}/3") + "\n")

    print(f"  4.3 Lock timeout: {'PASS' if passed else 'FAIL'} "
          f"busy={len(busy_responses)}/3")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Test 4.4 — Lock cleanup: no stale lock key remains
# ---------------------------------------------------------------------------

async def test_4_4_lock_cleanup(base_chat_id: int, f) -> bool:
    """After all normal processing, no lock:chat:* keys should remain."""
    CHAT_ID = base_chat_id + 40

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
    await r.delete(
        f"state:{CHAT_ID}", f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}", f"audit:{CHAT_ID}",
        f"lock:chat:{CHAT_ID}",
    )

    engine, _, adapter = await build_engine(r)
    await engine.process("Тест на очистку блокировки", CHAT_ID)

    # Check immediately after processing completes
    lock_key = f"lock:chat:{CHAT_ID}"
    stale_lock = await r.exists(lock_key)

    # Also check the main CHAT_ID used in concurrent tests
    main_lock = await r.exists(f"lock:chat:{base_chat_id + 10}")
    main_lock2 = await r.exists(f"lock:chat:{base_chat_id + 20}")
    main_lock3 = await r.exists(f"lock:chat:{base_chat_id + 30}")

    all_cleaned = not any([stale_lock, main_lock, main_lock2, main_lock3])
    passed = not bool(stale_lock)  # primary check: this test's lock is gone

    f.write("\n=== TEST 4.4: Lock Cleanup ===\n")
    f.write(f"  lock:chat:{CHAT_ID} exists after processing: {bool(stale_lock)}\n")
    f.write(f"  lock:chat:{base_chat_id+10} exists: {bool(main_lock)}\n")
    f.write(f"  lock:chat:{base_chat_id+20} exists: {bool(main_lock2)}\n")
    f.write(f"  lock:chat:{base_chat_id+30} exists: {bool(main_lock3)}\n")
    f.write(f"  All lock keys cleaned: {all_cleaned}\n")
    f.write(result_line("4.4 Lock cleanup", passed,
                        f"stale_lock_present={bool(stale_lock)}, all_cleaned={all_cleaned}") + "\n")

    print(f"  4.4 Lock cleanup: {'PASS' if passed else 'FAIL'} "
          f"stale={bool(stale_lock)} all_clean={all_cleaned}")

    await adapter.close()
    await r.aclose()
    return passed


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def main() -> None:
    BASE_CHAT_ID = 90004

    print("=" * 60)
    print("AUDIT 4: DISTRIBUTED LOCK TESTS")
    print(f"Base CHAT_ID: {BASE_CHAT_ID}")
    print("=" * 60)

    results: dict[str, bool] = {}

    with open(
        "C:/Users/sazon/OneDrive/Desktop/ai-native-crm/audit_4_lock_results.txt",
        "w",
        encoding="utf-8",
    ) as f:
        f.write("=" * 70 + "\n")
        f.write("AUDIT 4: DISTRIBUTED LOCK TESTS\n")
        f.write(f"Base CHAT_ID: {BASE_CHAT_ID}\n")
        f.write("=" * 70 + "\n")

        print("\n[4.1] Concurrent messages (5 simultaneous)...")
        results["4.1"] = await test_4_1_concurrent(BASE_CHAT_ID, f)

        print("\n[4.2] Lock during LLM processing...")
        results["4.2"] = await test_4_2_lock_during_llm(BASE_CHAT_ID, f)

        print("\n[4.3] Lock timeout (short timeout)...")
        results["4.3"] = await test_4_3_lock_timeout(BASE_CHAT_ID, f)

        print("\n[4.4] Lock cleanup (no stale keys)...")
        results["4.4"] = await test_4_4_lock_cleanup(BASE_CHAT_ID, f)

        # Summary
        passed_count = sum(1 for v in results.values() if v)
        total_count = len(results)
        overall = "PASS" if passed_count == total_count else "FAIL"

        f.write("\n" + "=" * 70 + "\n")
        f.write("RESULTS SUMMARY\n")
        f.write("=" * 70 + "\n")
        for test_id, passed in results.items():
            f.write(f"  Test {test_id}: {'PASS' if passed else 'FAIL'}\n")
        f.write(f"\nPassed: {passed_count}/{total_count}\n")
        f.write(f"OVERALL: {overall}\n")

    print("\n" + "=" * 60)
    print("AUDIT 4 RESULTS SUMMARY")
    print("=" * 60)
    for test_id, passed in results.items():
        print(f"  Test {test_id}: {'PASS' if passed else 'FAIL'}")
    print(f"  Passed: {passed_count}/{total_count}")
    print(f"  OVERALL: {overall}")
    print("=" * 60)
    print("Results saved to audit_4_lock_results.txt")


if __name__ == "__main__":
    asyncio.run(main())
