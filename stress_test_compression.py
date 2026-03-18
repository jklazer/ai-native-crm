"""
Стресс-тест компрессии: 15 ходов с принудительно низким token_budget.

Цель:
  - Убедиться, что компрессия срабатывает >= 2 раз при token_budget=50
  - Убедиться, что все critical facts выживают после компрессии
  - Убедиться, что процесс не падает

Результаты пишутся в stress_test_compression_results.txt
"""

import asyncio
import json
import os
import time

os.environ["PYTHONIOENCODING"] = "utf-8"


async def stress_test_compression() -> None:
    # ---------------------------------------------------------------------------
    # Патч token_budget ДО импорта engine (compressor читает settings в runtime,
    # поэтому достаточно выставить значение до первого needs_compression() вызова)
    # ---------------------------------------------------------------------------
    from ai_native_crm.config import settings

    original_token_budget = settings.token_budget
    settings.token_budget = 50  # очень низкий бюджет — форсируем компрессию

    # ---------------------------------------------------------------------------
    # Импорты и сборка движка (идентично stress_test.py)
    # ---------------------------------------------------------------------------
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

    CHAT_ID = 88888  # отдельный chat_id чтобы не конфликтовать со stress_test.py

    # Сбросить предыдущий стейт
    await r.delete(
        f"state:{CHAT_ID}",
        f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}",
        f"audit:{CHAT_ID}",
    )

    # ---------------------------------------------------------------------------
    # Сообщения — спроектированы давать БОЛЬШОЙ стейт:
    #   - длинные описания сделок со множеством деталей
    #   - несколько критических фактов в одном сообщении
    #   - запросы комплексного анализа (провоцируют большой new_working_memory)
    # ---------------------------------------------------------------------------
    messages = [
        # Сообщение 1: объёмное описание портфеля
        (
            "Полная картина по всем сделкам: нам нужно детально разобрать каждую. "
            "РосТех — крупнейший клиент, требует демо ERP-системы до конца недели, "
            "иначе переходят к SAP. Бюджет подтверждён, 1.2 млн руб. "
            "Дельта Софт просит скидку 15%, менеджер уже договорился на 10%. "
            "Альфа Логистик хочет модуль грузоотслеживания, бюджет до 1.1 млн. "
            "ЗАО Промтех: дедлайн 1 апреля, 1.5 млн, хотят три транша по 500к. "
            "ИП Сидорова — холодный лид, нет бюджета, риск отказа высокий."
        ),
        # Сообщение 2: критические факты + детали
        (
            "Зафиксируй все критические факты по переговорам: "
            "1) РосТех требует демо ERP до конца недели — это жёсткий дедлайн, иначе SAP; "
            "2) Дельта Софт бюджет ограничен 450к со скидкой 10%, выше не пойдут; "
            "3) Альфа Логистик: принимает решение только директор Смирнов, CFO не уполномочен; "
            "4) Промтех: оплата тремя траншами — обязательное условие договора; "
            "5) ИП Сидорова отказалась от CRM — денег нет совсем. "
            "Также проведи комплексный анализ рисков по каждой сделке с рекомендациями."
        ),
        # Сообщение 3: запрос детального анализа (длинный working_memory)
        (
            "Проведи глубокий стратегический анализ: по каждой активной сделке опиши "
            "текущую стадию, риски срыва, рекомендуемые следующие шаги, ключевых "
            "лиц принимающих решения, и потенциал допродаж. Особенно детально — "
            "по РосТех: нам нужно выиграть этот тендер любой ценой."
        ),
        # Сообщение 4: новые подробности + ещё critical facts
        (
            "Новые данные от отдела продаж: Альфа Логистик прислала ТЗ на 60 страниц, "
            "оценка трудозатрат — 1400 человекочасов, цена вырастет до 1.35 млн. "
            "Директор Смирнов лично звонил, ждёт КП до пятницы. "
            "Зафиксируй: Альфа Логистик — жёсткий дедлайн КП в пятницу, иначе выбирают 1С. "
            "По РосТех: после демо нужно предоставить референс-клиента из нефтянки. "
            "Обнови оценку ситуации по всему портфелю."
        ),
        # Сообщение 5: комплексный отчёт
        (
            "Подготовь исчерпывающий отчёт для директора: закрытые сделки за квартал, "
            "текущий pipeline с вероятностями закрытия, прогноз выручки на месяц и квартал, "
            "топ-3 риска портфеля, рекомендации по приоритизации усилий команды. "
            "Включи все критические факты которые влияют на прогноз."
        ),
        # Сообщение 6: детали переговоров
        (
            "Встреча с Промтех прошла хорошо: они готовы подписать договор, но добавили "
            "новое условие — гарантийный период 18 месяцев вместо стандартных 12. "
            "Юрист говорит это увеличит стоимость поддержки на 200к. "
            "Также: ЗАО Промтех хочет включить обучение 50 сотрудников в пакет. "
            "Зафиксируй эти требования как критические. Что рекомендуешь?"
        ),
        # Сообщение 7: статус и прогноз
        (
            "Еженедельный статус: детально по каждой сделке — что изменилось, "
            "какие риски выросли, какие снизились. Особенно интересует динамика по "
            "РосТех (демо прошло?) и Дельта Софт (договор подписан?). "
            "Также оцени вероятность выполнения плана продаж на этот месяц "
            "с учётом всех известных факторов."
        ),
        # Сообщение 8: новые критические факты
        (
            "Важные обновления: "
            "РосТех — демо прошло отлично, они просят коммерческое предложение на расширенный пакет с модулем HR, "
            "общая сумма может вырасти до 1.8 млн. Контакт — Петров Игорь Александрович, CTO. "
            "Дельта Софт — договор подписан на 405000 руб, деньги ожидаются на следующей неделе. "
            "Зафиксируй: РосТех расширяет запрос, потенциал сделки 1.8 млн. "
            "Зафиксируй: Дельта Софт закрыта успешно на 405к."
        ),
        # Сообщение 9: анализ закрытых сделок
        (
            "Проанализируй почему Дельта Софт удалось закрыть: какие факторы сыграли роль, "
            "что можно применить к другим сделкам. Также: какие уроки из переговоров по РосТех "
            "можно использовать при работе с другими крупными клиентами? "
            "Составь детальный список best practices для команды продаж."
        ),
        # Сообщение 10: планирование
        (
            "Планирование следующей недели: составь подробный план действий по каждой "
            "активной сделке с конкретными шагами, ответственными, дедлайнами. "
            "Учти все критические факты и риски. "
            "Приоритизируй: что нужно сделать обязательно в понедельник, "
            "что во вторник-среду, что можно перенести."
        ),
        # Сообщение 11: новый крупный лид
        (
            "Новый крупный лид: ПАО Газпром-Медиа интересуется внедрением CRM для "
            "отдела продаж рекламы, 150 пользователей. Контакт — Волкова Наталья Сергеевна, "
            "директор по цифровизации. Бюджет предварительный — до 5 млн руб. "
            "Они уже смотрели Salesforce и Microsoft Dynamics. "
            "Зафиксируй как стратегический лид. Что нужно подготовить для первой встречи?"
        ),
        # Сообщение 12: детальный анализ конкуренции
        (
            "Конкурентный анализ по РосТех: они сравнивают нас с SAP Business One и 1С:ERP. "
            "Наши преимущества: гибкость, скорость внедрения, цена. "
            "Недостатки: нет модуля управления производством уровня SAP. "
            "Как правильно позиционироваться? Какие аргументы использовать? "
            "Зафиксируй: РосТех сравнивает с SAP Business One и 1С:ERP — нужна конкурентная аргументация."
        ),
        # Сообщение 13: квартальный итог
        (
            "Квартальный итог: суммируй все закрытые сделки, текущий pipeline, прогноз. "
            "Какова итоговая выручка квартала? Выполнен ли план? "
            "Перечисли все критические факты которые накопились за квартал. "
            "Какие системные выводы можно сделать о нашей воронке продаж?"
        ),
        # Сообщение 14: расширенный анализ
        (
            "Глубокий анализ воронки: на каком этапе мы теряем больше всего сделок? "
            "Какие типы клиентов наиболее перспективны? Какой средний цикл сделки? "
            "Проанализируй паттерны по всем сделкам которые мы обсуждали. "
            "Дай конкретные рекомендации по улучшению конверсии."
        ),
        # Сообщение 15: финальное резюме
        (
            "Финальное резюме всего диалога: ключевые достижения, открытые вопросы, "
            "критические факты по каждой сделке, следующие шаги. "
            "Это резюме должно быть максимально подробным — оно пойдёт в отчёт директору. "
            "Включи все детали по РосТех, Промтех, Альфа Логистик, Газпром-Медиа."
        ),
    ]

    # ---------------------------------------------------------------------------
    # Счётчики для PASS/FAIL
    # ---------------------------------------------------------------------------
    compressions_triggered = 0
    facts_before_first_compression: list | None = None
    no_crashes = True
    turn_logs: list[dict] = []

    # Инструментируем compressor для подсчёта срабатываний
    original_compress = compressor.compress

    async def counting_compress(state):
        nonlocal compressions_triggered, facts_before_first_compression
        compressions_triggered += 1
        if facts_before_first_compression is None:
            facts_before_first_compression = await store.get_critical_facts(CHAT_ID)
        return await original_compress(state)

    compressor.compress = counting_compress

    # ---------------------------------------------------------------------------
    # Прогон 15 ходов
    # ---------------------------------------------------------------------------
    print(f"token_budget patched to: {settings.token_budget}")
    print("Starting compression stress test (15 turns)...\n")

    with open("stress_test_compression_results.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"COMPRESSION STRESS TEST: 15 turns, token_budget={settings.token_budget}\n")
        f.write("=" * 80 + "\n\n")

        for i, msg in enumerate(messages, 1):
            compressions_before = compressions_triggered
            t0 = time.time()
            try:
                resp = await engine.process(msg, CHAT_ID)
            except Exception as e:
                resp = f"ERROR: {e}"
                no_crashes = False
            elapsed = time.time() - t0

            state = await store.load(CHAT_ID)
            facts = await store.get_critical_facts(CHAT_ID)

            # Размер стейта в символах и примерных токенах
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

            turn_log = {
                "turn": i,
                "elapsed_s": round(elapsed, 1),
                "state_chars": state_size_chars,
                "token_est": token_est,
                "compression_triggered": compression_this_turn,
                "facts_count": len(facts),
                "iteration": state.iteration,
            }
            turn_logs.append(turn_log)

            f.write(f"--- Turn {i} ({elapsed:.1f}s) ---\n")
            f.write(f"Q: {msg[:120]}{'...' if len(msg) > 120 else ''}\n")
            f.write(f"A: {resp[:300]}{'...' if len(resp) > 300 else ''}\n")
            f.write(
                f"State: {state_size_chars} chars (~{token_est} tok) | "
                f"iter={state.iteration} | facts={len(facts)} | "
                f"compression={'YES' if compression_this_turn else 'no'}\n"
            )
            f.write(f"WM: {state.working_memory[:200]}\n")
            f.write(f"Summary: {state.conversation_summary[:150]}\n\n")
            f.flush()

            status = "COMPRESS!" if compression_this_turn else "       "
            print(
                f"Turn {i:2d}/15 | {elapsed:5.1f}s | "
                f"~{token_est:4d}tok | facts={len(facts)} | {status}"
            )

        # ---------------------------------------------------------------------------
        # Финальная проверка
        # ---------------------------------------------------------------------------
        final_state = await store.load(CHAT_ID)
        final_facts = await store.get_critical_facts(CHAT_ID)

        # Проверка: все факты до первой компрессии дожили до конца
        facts_survived = True
        if facts_before_first_compression is not None:
            pre_compression_contents = {
                (fc.content, fc.deal_id) for fc in facts_before_first_compression
            }
            final_contents = {(fc.content, fc.deal_id) for fc in final_facts}
            lost = pre_compression_contents - final_contents
            if lost:
                facts_survived = False

        # ---------------------------------------------------------------------------
        # PASS/FAIL вердикт
        # ---------------------------------------------------------------------------
        compressions_pass = compressions_triggered >= 2
        facts_pass = facts_survived
        crashes_pass = no_crashes

        overall = "PASS" if (compressions_pass and facts_pass and crashes_pass) else "FAIL"

        summary_lines = [
            "",
            "=" * 80,
            "RESULTS SUMMARY",
            "=" * 80,
            f"token_budget during test : {settings.token_budget}",
            f"Total turns              : {len(messages)}",
            f"Compressions triggered   : {compressions_triggered}",
            f"Critical facts at end    : {len(final_facts)}",
            f"Final state iteration    : {final_state.iteration}",
            "",
            "PASS/FAIL CHECKS:",
            f"  compressions >= 2      : {'PASS' if compressions_pass else 'FAIL'} "
            f"(got {compressions_triggered})",
            f"  all critical facts survived: {'PASS' if facts_pass else 'FAIL'}",
            f"  no crashes             : {'PASS' if crashes_pass else 'FAIL'}",
            "",
            f"OVERALL: {overall}",
            "",
        ]

        if final_facts:
            summary_lines.append(f"All critical facts ({len(final_facts)}):")
            for idx, fact in enumerate(final_facts, 1):
                line = f"  {idx}. [{fact.fact_type}] {fact.content}"
                if fact.deal_id:
                    line += f" (deal {fact.deal_id})"
                summary_lines.append(line)
        else:
            summary_lines.append("No critical facts found.")

        summary_lines += [
            "",
            f"Final working_memory ({len(final_state.working_memory)} chars):",
            final_state.working_memory,
            "",
            f"Final conversation_summary ({len(final_state.conversation_summary)} chars):",
            final_state.conversation_summary,
        ]

        summary_text = "\n".join(summary_lines)
        f.write(summary_text + "\n")

    # ---------------------------------------------------------------------------
    # Вывод в консоль
    # ---------------------------------------------------------------------------
    print()
    print("=" * 60)
    print("COMPRESSION STRESS TEST RESULTS")
    print("=" * 60)
    print(f"  compressions >= 2  : {'PASS' if compressions_pass else 'FAIL'} ({compressions_triggered})")
    print(f"  facts survived     : {'PASS' if facts_pass else 'FAIL'}")
    print(f"  no crashes         : {'PASS' if crashes_pass else 'FAIL'}")
    print(f"  OVERALL            : {overall}")
    print("=" * 60)
    print("Results saved to stress_test_compression_results.txt")

    # Восстановить оригинальный token_budget
    settings.token_budget = original_token_budget

    await adapter.close()
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(stress_test_compression())
