"""
AUDIT 1: Comprehensive 30-Turn Stress Test
==========================================
Tests memory durability, compression survival, action routing,
drift detection, and working memory coherence over 30 turns.

Scenario:
  Turns 1-5:   3 new leads arrive, manager asks about each
  Turn 6:      Lead #1 client says "budget max 300k" → critical fact
  Turn 7:      Manager: переведи сделку X в стадию переговоров → update_deal
  Turn 8:      Lead #2 client REFUSED → critical fact: rejection
  Turns 9-12:  Discussion about deal #1 details, discount, terms
  Turn 13:     Manager asks about budget of first client → MUST recall 300k
  Turn 14:     Force compression (token_budget=30)
  Turn 15:     Manager asks if second client refused → MUST recall rejection
  Turns 16-20: New leads, closing one deal
  Turn 21:     Second compression (token_budget=30)
  Turn 22:     Manager asks about client from turn 6 → verify 300k survived 2 compressions
  Turns 23-30: Mix of new deals, updates, questions about old ones

Checks:
  1. Critical fact "budget 300k" survived both compressions
  2. Critical fact "rejection" survived both compressions
  3. Drift score after 30 turns
  4. State did not explode (track tokens per turn)
  5. Working memory is coherent (not garbage)
  6. All actions executed successfully
  7. conversation_summary is populated
  8. No duplicate critical facts
"""

import asyncio
import json
import os
import sys
import time

os.environ["PYTHONIOENCODING"] = "utf-8"

# Force stdout/stderr to UTF-8 on Windows so Cyrillic + ruble sign (₽) don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def safe_print(*args, **kwargs):
    """Print with unicode-safe fallback for Windows consoles."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        print(text.encode("utf-8", errors="replace").decode("ascii", errors="replace"), **kwargs)

CHAT_ID = 90001
RESULTS_FILE = "audit_1_stress_results.txt"


async def run_stress_test() -> None:
    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------
    from ai_native_crm.config import settings
    import redis.asyncio as aioredis
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

    original_token_budget = settings.token_budget

    # ------------------------------------------------------------------
    # Build engine
    # ------------------------------------------------------------------
    r = aioredis.from_url("redis://localhost:6379/5", decode_responses=True)
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

    # ------------------------------------------------------------------
    # Reset state for this test chat_id
    # ------------------------------------------------------------------
    await r.delete(
        f"state:{CHAT_ID}",
        f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}",
        f"audit:{CHAT_ID}",
    )

    # ------------------------------------------------------------------
    # Fetch real deal IDs from Bitrix
    # ------------------------------------------------------------------
    safe_print("Fetching real deals from Bitrix24...")
    deals = await adapter.get_deals()
    safe_print(f"  Got {len(deals)} deals from Bitrix24")
    for d in deals[:5]:
        safe_print(f"    Deal ID={d.id} title={d.title!r} stage={d.stage} amount={d.amount}")

    # Use real deal IDs if available, fall back to safe placeholders
    deal_id_1 = deals[0].id if len(deals) > 0 else "1"
    deal_id_2 = deals[1].id if len(deals) > 1 else "2"
    deal_id_3 = deals[2].id if len(deals) > 2 else "3"

    deal_title_1 = deals[0].title if len(deals) > 0 else "Сделка 1"
    deal_title_2 = deals[1].title if len(deals) > 1 else "Сделка 2"
    deal_title_3 = deals[2].title if len(deals) > 2 else "Сделка 3"

    safe_print(f"\nUsing deal IDs: {deal_id_1}, {deal_id_2}, {deal_id_3}")
    safe_print(f"Titles: {deal_title_1!r}, {deal_title_2!r}, {deal_title_3!r}\n")

    # ------------------------------------------------------------------
    # Compression tracking
    # ------------------------------------------------------------------
    compressions_triggered = 0
    compression_turns: list[int] = []
    original_compress = compressor.compress

    async def counting_compress(state):
        nonlocal compressions_triggered
        compressions_triggered += 1
        return await original_compress(state)

    compressor.compress = counting_compress

    # Action success tracking
    total_actions = 0
    successful_actions = 0
    action_log: list[dict] = []

    # ------------------------------------------------------------------
    # Build 30-turn scenario using real deal IDs
    # ------------------------------------------------------------------
    messages = [
        # ---- Turns 1-5: 3 leads arrive, manager asks about each ----
        # Turn 1
        (
            f"Новый лид пришёл: компания Альфа-Тех, контакт Иванов Сергей, "
            f"интересуются CRM-системой для отдела продаж 50 человек. "
            f"Это сделка {deal_id_1} ({deal_title_1}). "
            f"Расскажи что можешь по этой сделке и что нужно сделать в первую очередь?"
        ),
        # Turn 2
        (
            f"Второй новый лид: ООО Бета-Сервис, контакт Петрова Анна, "
            f"нужна автоматизация документооборота. "
            f"Это сделка {deal_id_2} ({deal_title_2}). "
            f"Какова текущая стадия и что с ней делать дальше?"
        ),
        # Turn 3
        (
            f"Третий лид: ЗАО Гамма Логистик, директор Козлов Михаил, "
            f"хотят внедрить модуль склада и грузоотслеживания. "
            f"Это сделка {deal_id_3} ({deal_title_3}). "
            f"Оцени перспективность этого лида."
        ),
        # Turn 4
        (
            f"Ещё раз по первым двум: сделка {deal_id_1} и сделка {deal_id_2}. "
            f"Какие у них текущие суммы и стадии? "
            f"Кто из них более приоритетный на этой неделе?"
        ),
        # Turn 5
        (
            f"Дай общую картину по всем трём лидам — {deal_id_1}, {deal_id_2}, {deal_id_3}. "
            f"Расставь приоритеты и скажи с кого начать работу."
        ),
        # ---- Turn 6: Client from lead #1 says budget max 300k ----
        (
            f"Только что говорил с клиентом по сделке {deal_id_1} — Иванов сказал, "
            f"что бюджет у них максимум 300 тысяч рублей, выше не пойдут ни при каких условиях. "
            f"Это жёсткое ограничение. Зафикси это как критический факт. "
            f"Что теперь делать с этой сделкой?"
        ),
        # ---- Turn 7: Manager asks to move deal to negotiation stage ----
        (
            f"Переведи сделку {deal_id_1} в стадию переговоров. "
            f"После обновления скажи текущий статус."
        ),
        # ---- Turn 8: Client from lead #2 REFUSED ----
        (
            f"Клиент по сделке {deal_id_2} — Петрова Анна — только что написала: "
            f"'не интересно, у нас уже есть другой поставщик'. Полный отказ. "
            f"Зафикси отказ как критический факт. Что делать с этой сделкой?"
        ),
        # ---- Turns 9-12: Discussion about deal #1 details ----
        # Turn 9
        (
            f"По сделке {deal_id_1}: если бюджет 300к — можем ли мы уложиться? "
            f"Что войдёт в пакет за эту сумму? Стоит ли давать скидку?"
        ),
        # Turn 10
        (
            f"Иванов из {deal_id_1} спрашивает о вариантах оплаты: "
            f"они хотят разбить платёж на три транша. "
            f"Это приемлемо? Как это влияет на условия договора?"
        ),
        # Turn 11
        (
            f"Есть ли риск что {deal_id_1} сорвётся? "
            f"Учти все известные факты: бюджет 300к, три транша. "
            f"Какова вероятность закрытия?"
        ),
        # Turn 12
        (
            f"Подготовь коммерческое предложение для сделки {deal_id_1} "
            f"с учётом бюджета клиента. Какие пункты обязательно включить? "
            f"Зафикси условия КП как важный факт."
        ),
        # ---- Turn 13: Manager MUST recall 300k from critical facts ----
        (
            "Напомни — какой был бюджет у первого клиента (Иванов, Альфа-Тех)? "
            "Я забыл точную цифру. Это критически важно для подготовки договора."
        ),
        # ---- Turn 14: Force compression (patched before this turn) ----
        (
            f"Сделай полный срез по всем активным переговорам. "
            f"По каждой сделке: стадия, сумма, ключевые факты, следующие шаги. "
            f"Особенно важно по {deal_id_1} и {deal_id_3}."
        ),
        # ---- Turn 15: Manager MUST recall rejection from critical facts ----
        (
            "Тот второй клиент — Бета-Сервис — он же отказался, верно? "
            "Хочу убедиться прежде чем убирать из воронки."
        ),
        # ---- Turns 16-20: New leads, closing one deal ----
        # Turn 16
        (
            "Новый крупный лид: ПАО Энерго-Строй, бюджет до 1.5 млн руб, "
            "хотят полную автоматизацию отдела продаж и склада. "
            "Контакт — Директор Волков Николай. Оцени потенциал."
        ),
        # Turn 17
        (
            "Связался с Козловым из Гамма Логистик (третий лид). "
            "Они готовы к демонстрации продукта на следующей неделе. "
            "Зафикси: Гамма Логистик согласны на демо, дедлайн — следующая неделя. "
            "Что подготовить к демо?"
        ),
        # Turn 18
        (
            f"Хорошие новости по сделке {deal_id_3}: после анализа ТЗ "
            f"они готовы к подписанию предварительного договора. "
            f"Обнови стадию сделки {deal_id_3} — переведи в стадию финальных переговоров."
        ),
        # Turn 19
        (
            "Итоги недели: сколько сделок в работе, какова общая сумма pipeline? "
            "Расставь по приоритету все активные сделки."
        ),
        # Turn 20
        (
            f"Хочу закрыть одну из сделок — {deal_id_3} подходит? "
            f"Подтверди финальные условия и скажи что нужно для закрытия."
        ),
        # ---- Turn 21: Second compression ----
        (
            "Дай полный отчёт по воронке продаж: все активные сделки, "
            "критические факты, риски и рекомендации на следующий месяц. "
            "Максимально подробно — это для директора."
        ),
        # ---- Turn 22: Verify 300k survived 2 compressions ----
        (
            "Вернёмся к клиенту Иванову из Альфа-Тех — помнишь каков был их "
            "максимальный бюджет? Мне нужна точная цифра для финального КП."
        ),
        # ---- Turns 23-30: Mix of deals, questions about old ones ----
        # Turn 23
        (
            "Новый запрос поступил от старого клиента — ООО ТехМаш, "
            "хотят расширить лицензию на 20 дополнительных пользователей. "
            "Как обработать апсейл?"
        ),
        # Turn 24
        (
            f"По сделке {deal_id_1}: Иванов написал что готов обсудить "
            f"увеличение бюджета до 350к если мы добавим модуль аналитики. "
            f"Это выше их изначального лимита 300к — как относиться к этому? "
            f"Стоит ли обновить критический факт о бюджете?"
        ),
        # Turn 25
        (
            "Конкурент предложил Гамма Логистик аналогичное решение на 15% дешевле. "
            "Что делать? Давать ли контрпредложение?"
        ),
        # Turn 26
        (
            "Сводка по критическим фактам: перечисли все зафиксированные "
            "критические факты по всем сделкам — бюджеты, отказы, дедлайны, условия."
        ),
        # Turn 27
        (
            "Есть ли дубликаты в критических фактах? Проверь нет ли одинаковых "
            "записей — мне нужен чистый список без повторов."
        ),
        # Turn 28
        (
            f"Финальный вопрос по сделке {deal_id_2} (Бета-Сервис): "
            f"стоит ли попытаться реанимировать её через 3 месяца "
            f"или это окончательный отказ?"
        ),
        # Turn 29
        (
            "Общий итог по всем переговорам за этот период: "
            "сколько выиграли, сколько проиграли, какой общий объём pipeline. "
            "Включи оценку работы команды продаж."
        ),
        # Turn 30
        (
            "Финальное резюме всего диалога: ключевые события, "
            "все критические факты, открытые сделки и следующие шаги. "
            "Это идёт в еженедельный отчёт."
        ),
    ]

    assert len(messages) == 30, f"Expected 30 messages, got {len(messages)}"

    # ------------------------------------------------------------------
    # Turns that trigger forced compression (patch token_budget=30)
    # ------------------------------------------------------------------
    FORCE_COMPRESS_TURNS = {14, 21}

    # ------------------------------------------------------------------
    # Run the test
    # ------------------------------------------------------------------
    turn_logs: list[dict] = []
    no_crashes = True
    t_total_start = time.time()

    safe_print(f"CHAT_ID={CHAT_ID}")
    safe_print(f"Running 30-turn stress test...\n")
    safe_print(f"{'Turn':>5} | {'Time':>6} | {'~Tokens':>7} | {'Facts':>5} | {'Compress':>8} | Response[:60]")
    safe_print("-" * 100)

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("AUDIT 1: 30-TURN COMPREHENSIVE STRESS TEST\n")
        f.write(f"CHAT_ID={CHAT_ID}\n")
        f.write(f"Deal IDs used: {deal_id_1}, {deal_id_2}, {deal_id_3}\n")
        f.write("=" * 80 + "\n\n")

        for turn_num, msg in enumerate(messages, 1):
            # Patch token_budget for forced compression turns
            forced_compression = turn_num in FORCE_COMPRESS_TURNS
            if forced_compression:
                settings.token_budget = 30
                safe_print(f"  [!] Turn {turn_num}: token_budget patched to 30 (force compression)")

            compressions_before = compressions_triggered
            t0 = time.time()

            try:
                resp = await engine.process(msg, CHAT_ID)
            except Exception as e:
                resp = f"ERROR: {e}"
                no_crashes = False
                safe_print(f"  [ERROR] Turn {turn_num}: {e}")

            elapsed = time.time() - t0

            # Restore token_budget after forced compression turns
            if forced_compression:
                settings.token_budget = original_token_budget
                safe_print(f"  [!] Turn {turn_num}: token_budget restored to {original_token_budget}")

            # Gather state metrics
            state = await store.load(CHAT_ID)
            facts = await store.get_critical_facts(CHAT_ID)

            state_json = json.dumps(
                {
                    "working_memory": state.working_memory,
                    "agent_assessment": state.agent_assessment,
                    "conversation_summary": state.conversation_summary,
                },
                ensure_ascii=False,
            )
            state_size_chars = len(state_json)
            token_est = state_size_chars // 3
            compression_this_turn = compressions_triggered > compressions_before
            if compression_this_turn:
                compression_turns.append(turn_num)

            # Track action results from audit
            audit_entries = await store.get_audit(CHAT_ID, limit=1)
            if audit_entries:
                last_entry = audit_entries[-1]
                for act in last_entry.get("actions", []):
                    if act.get("type") != "_validator_alerts":
                        total_actions += 1
                        if act.get("success", False):
                            successful_actions += 1
                        action_log.append({
                            "turn": turn_num,
                            "type": act.get("type"),
                            "success": act.get("success"),
                        })

            turn_log = {
                "turn": turn_num,
                "elapsed_s": round(elapsed, 1),
                "state_chars": state_size_chars,
                "token_est": token_est,
                "compression_triggered": compression_this_turn,
                "facts_count": len(facts),
                "iteration": state.iteration,
                "forced_compression": forced_compression,
                "response_snippet": resp[:300],
            }
            turn_logs.append(turn_log)

            compress_mark = "COMPRESS!" if compression_this_turn else ("FORCE" if forced_compression else "       ")
            resp_short = resp[:60].replace("\n", " ")
            safe_print(
                f"{turn_num:5d} | {elapsed:5.1f}s | {token_est:7d} | {len(facts):5d} | "
                f"{compress_mark:8} | {resp_short}"
            )

            # Write to results file
            f.write(f"{'='*60}\n")
            f.write(f"Turn {turn_num}/30 | {elapsed:.1f}s | ~{token_est} tokens | facts={len(facts)}\n")
            if forced_compression:
                f.write(f"[FORCED COMPRESSION TURN - token_budget patched to 30]\n")
            f.write(f"Q: {msg[:200]}{'...' if len(msg) > 200 else ''}\n")
            f.write(f"A: {resp[:300]}{'...' if len(resp) > 300 else ''}\n")
            f.write(
                f"State: {state_size_chars} chars (~{token_est} tok) | "
                f"iter={state.iteration} | facts={len(facts)} | "
                f"compression={'YES' if compression_this_turn else 'no'}\n"
            )
            f.write(f"WM (first 200): {state.working_memory[:200]}\n")
            f.write(f"Summary (first 150): {state.conversation_summary[:150]}\n\n")
            f.flush()

        # ------------------------------------------------------------------
        # Final checks
        # ------------------------------------------------------------------
        total_elapsed = time.time() - t_total_start
        final_state = await store.load(CHAT_ID)
        final_facts = await store.get_critical_facts(CHAT_ID)

        # Check 1: budget 300k survived both compressions
        budget_300k_found = any(
            "300" in fc.content for fc in final_facts
        )

        # Check 2: rejection survived both compressions
        rejection_found = any(
            fc.fact_type == "rejection" or
            any(kw in fc.content.lower() for kw in ["отказ", "не интересно", "refused", "rejection"])
            for fc in final_facts
        )

        # Check 3: drift score
        drift_score = await drift.check(final_state)

        # Check 4: state didn't explode (max tokens across all turns)
        max_tokens = max(tl["token_est"] for tl in turn_logs)
        token_explosion = max_tokens > 10_000

        # Check 5: working memory coherence
        wm_coherent = (
            len(final_state.working_memory) > 50 and
            final_state.working_memory != "(пусто)" and
            not final_state.working_memory.startswith("ERROR")
        )

        # Check 6: action success rate
        action_success_rate = (successful_actions / total_actions) if total_actions > 0 else 1.0
        actions_ok = action_success_rate >= 0.85

        # Check 7: conversation_summary populated
        summary_populated = (
            len(final_state.conversation_summary) > 30 and
            final_state.conversation_summary != "(нет резюме)"
        )

        # Check 8: no duplicate critical facts
        seen_contents: set[tuple[str, str | None]] = set()
        duplicate_count = 0
        for fc in final_facts:
            key = (fc.content, fc.deal_id)
            if key in seen_contents:
                duplicate_count += 1
            else:
                seen_contents.add(key)
        no_duplicates = duplicate_count == 0

        # ------------------------------------------------------------------
        # PASS/FAIL verdicts
        # ------------------------------------------------------------------
        checks = {
            "budget_300k_survived_2_compressions": budget_300k_found,
            "rejection_survived_2_compressions": rejection_found,
            "drift_score_below_threshold": drift_score < 0.40,
            "state_no_explosion": not token_explosion,
            "working_memory_coherent": wm_coherent,
            "action_success_rate_ok": actions_ok,
            "conversation_summary_populated": summary_populated,
            "no_duplicate_critical_facts": no_duplicates,
            "no_crashes": no_crashes,
        }

        all_pass = all(checks.values())
        overall = "PASS" if all_pass else "FAIL"

        # ------------------------------------------------------------------
        # Write summary to file
        # ------------------------------------------------------------------
        summary_lines = [
            "",
            "=" * 80,
            "FINAL RESULTS SUMMARY",
            "=" * 80,
            f"Total turns           : 30",
            f"Total elapsed         : {total_elapsed:.1f}s",
            f"Compressions total    : {compressions_triggered}",
            f"Compression turns     : {compression_turns}",
            f"Critical facts at end : {len(final_facts)}",
            f"Duplicate facts       : {duplicate_count}",
            f"Final iteration       : {final_state.iteration}",
            f"Max tokens/turn       : {max_tokens}",
            f"Total actions         : {total_actions}",
            f"Successful actions    : {successful_actions}",
            f"Action success rate   : {action_success_rate:.1%}",
            f"Drift score (final)   : {drift_score:.3f}",
            "",
            "PASS/FAIL CHECKS:",
        ]

        for check_name, result in checks.items():
            mark = "PASS" if result else "FAIL"
            summary_lines.append(f"  {mark}  {check_name}")

        summary_lines += [
            "",
            f"OVERALL: {overall}",
            "",
        ]

        # List all critical facts
        if final_facts:
            summary_lines.append(f"All critical facts ({len(final_facts)}):")
            for idx, fact in enumerate(final_facts, 1):
                line = f"  {idx:2d}. [{fact.fact_type}] {fact.content}"
                if fact.deal_id:
                    line += f" (deal {fact.deal_id})"
                summary_lines.append(line)
        else:
            summary_lines.append("WARNING: No critical facts found!")

        # Token progression
        summary_lines += [
            "",
            "Token progression per turn:",
        ]
        for tl in turn_logs:
            mark = " <COMPRESS>" if tl["compression_triggered"] else (" <FORCED>" if tl["forced_compression"] else "")
            summary_lines.append(
                f"  Turn {tl['turn']:2d}: ~{tl['token_est']:5d} tok | facts={tl['facts_count']}{mark}"
            )

        # Action log
        if action_log:
            summary_lines += ["", "Actions log:"]
            for al in action_log:
                status = "OK" if al["success"] else "FAIL"
                summary_lines.append(f"  Turn {al['turn']:2d}: [{status}] {al['type']}")

        # Final state details
        summary_lines += [
            "",
            f"Final working_memory ({len(final_state.working_memory)} chars):",
            final_state.working_memory,
            "",
            f"Final conversation_summary ({len(final_state.conversation_summary)} chars):",
            final_state.conversation_summary,
            "",
            f"Final agent_assessment ({len(final_state.agent_assessment)} chars):",
            final_state.agent_assessment[:500],
        ]

        summary_text = "\n".join(summary_lines)
        f.write(summary_text + "\n")

    # ------------------------------------------------------------------
    # Console output
    # ------------------------------------------------------------------
    safe_print("\n" + "=" * 80)
    safe_print("AUDIT 1 STRESS TEST -- FINAL RESULTS")
    safe_print("=" * 80)
    safe_print(f"  Total turns          : 30")
    safe_print(f"  Total time           : {total_elapsed:.1f}s")
    safe_print(f"  Compressions total   : {compressions_triggered} (turns: {compression_turns})")
    safe_print(f"  Critical facts total : {len(final_facts)}")
    safe_print(f"  Max tokens/turn      : {max_tokens}")
    safe_print(f"  Drift score (final)  : {drift_score:.3f}")
    safe_print(f"  Action success rate  : {action_success_rate:.1%} ({successful_actions}/{total_actions})")
    safe_print()
    safe_print("PASS/FAIL CHECKS:")
    for check_name, result in checks.items():
        mark = "PASS" if result else "FAIL"
        safe_print(f"  {mark}  {check_name}")
    safe_print()
    safe_print(f"OVERALL: {overall}")
    safe_print("=" * 80)
    safe_print(f"Full results saved to {RESULTS_FILE}")

    await adapter.close()
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(run_stress_test())
