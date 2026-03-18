"""
Стресс-тест: 30 ходов агента с реальным Bitrix24 + GPT-4o-mini.
Проверяет эволюцию стейта, компрессию, critical facts, drift.
"""
import asyncio
import json
import time
import sys
import os

os.environ["PYTHONIOENCODING"] = "utf-8"


async def stress_test():
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

    CHAT_ID = 77777
    # Clear previous state
    await r.delete(
        f"state:{CHAT_ID}",
        f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}",
        f"audit:{CHAT_ID}",
    )

    messages = [
        # Day 1: Morning
        "Доброе утро, покажи все текущие сделки и их стадии",
        "Какая общая сумма по всем активным сделкам?",
        "Клиент Козлов Дмитрий Петрович из Дельта Софт звонил, просит скидку 15%. Тел +79161234567",
        "Зафиксируй: РосТех требует демо ERP до конца недели, иначе уходят к SAP",
        "Какие сделки на стадии выставления счета?",
        # Day 1: Afternoon
        "ИП Сидорова - новый лид, нужно назначить встречу на завтра",
        "Альфа Логистик хочет добавить модуль отслеживания грузов, бюджет до 1.1 млн",
        "Обнови сделку с ГазпромНефть на 5 млн",
        "Подготовь краткий отчет для руководства по итогам дня",
        "Какие сделки рискуют сорваться?",
        # Day 2
        "Утренний статус: что изменилось с вчера?",
        "ЗАО Промтех подтвердил дедлайн 1 апреля, бюджет 1.5 млн",
        "РосТех прислал ТЗ на 47 страниц, нужно оценить трудозатраты",
        "Сколько сделок на стадии квалификации?",
        "Подготовь прогноз выручки на этот месяц",
        # Day 2: Afternoon
        "Новый лид: Техносервис, документооборот, контакт Николаев Сергей +79031112233",
        "Напомни про встречу с Альфа Логистик через 2 часа",
        "Какая конверсия из квалификации в счет?",
        "Дельта Софт согласился на 10% скидку вместо 15%",
        "Какой статус по критическим фактам?",
        # Day 3
        "Недельный обзор: прогресс по каждой сделке",
        "РосТех: после ТЗ оценка 1200 человекочасов, цена 1.2 млн",
        "Промтех просит разбить оплату на 3 транша по 500к",
        "Критический факт: ИП Сидорова отказалась от CRM, бюджета нет",
        "Обнови общую картину - какие сделки движутся, какие стоят?",
        # Day 3: Closing
        "Дельта Софт подписала договор! Сумма 405000 руб со скидкой 10%",
        "Подготовь отчет для директора: закрытые сделки и pipeline",
        "Какие действия нужны на следующей неделе по каждой сделке?",
        "Итого за неделю: закрытые сделки, pipeline, прогноз",
        "Спасибо, сохрани всю информацию",
    ]

    with open("stress_test_results.txt", "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("STRESS TEST: 30 turns, real Bitrix24 + GPT-4o-mini\n")
        f.write("=" * 80 + "\n\n")

        for i, msg in enumerate(messages, 1):
            t0 = time.time()
            try:
                resp = await engine.process(msg, CHAT_ID)
            except Exception as e:
                resp = f"ERROR: {e}"
            elapsed = time.time() - t0

            state = await store.load(CHAT_ID)
            facts = await store.get_critical_facts(CHAT_ID)
            state_json = json.dumps(
                {
                    "iteration": state.iteration,
                    "working_memory": state.working_memory,
                    "agent_assessment": state.agent_assessment,
                    "conversation_summary": state.conversation_summary,
                },
                ensure_ascii=False,
            )
            state_size = len(state_json)
            token_est = state_size // 3

            f.write(f"--- Turn {i} ({elapsed:.1f}s) ---\n")
            f.write(f"Q: {msg}\n")
            f.write(f"A: {resp}\n")
            f.write(
                f"State: {state_size} chars (~{token_est} tok), "
                f"iter={state.iteration}, facts={len(facts)}\n"
            )
            f.write(f"WM: {state.working_memory[:200]}\n")
            f.write(f"Assessment: {state.agent_assessment[:200]}\n\n")
            f.flush()

            print(
                f"Turn {i:2d}/30 | {elapsed:5.1f}s | "
                f"state={state_size:5d}ch ~{token_est:4d}tok | "
                f"facts={len(facts)} | iter={state.iteration}"
            )

        # Final summary
        f.write("\n" + "=" * 80 + "\n")
        f.write("SUMMARY\n")
        f.write("=" * 80 + "\n\n")

        final_state = await store.load(CHAT_ID)
        drift_score = await drift.check(final_state)
        all_facts = await store.get_critical_facts(CHAT_ID)
        final_metrics = await store.get_metrics(CHAT_ID)

        f.write(f"Final drift score: {drift_score}\n")
        f.write(f"Final iteration: {final_state.iteration}\n")
        f.write(f"Final facts count: {len(all_facts)}\n")
        state_full = json.dumps(final_state.__dict__, ensure_ascii=False)
        f.write(f"Final state size: {len(state_full)} chars\n")

        f.write(f"\nAll critical facts ({len(all_facts)}):\n")
        for idx, fact in enumerate(all_facts, 1):
            line = f"  {idx}. [{fact.fact_type}] {fact.content}"
            if fact.deal_id:
                line += f" (deal {fact.deal_id})"
            f.write(line + "\n")

        f.write(f"\nFinal metrics: {json.dumps(final_metrics)}\n")
        f.write(f"\nFinal working_memory:\n{final_state.working_memory}\n")
        f.write(f"\nFinal agent_assessment:\n{final_state.agent_assessment}\n")
        f.write(
            f"\nFinal conversation_summary:\n{final_state.conversation_summary}\n"
        )

        print(f"\nDrift: {drift_score}")
        print(f"Facts: {len(all_facts)}")
        print(f"Metrics: {json.dumps(final_metrics)}")

    await adapter.close()
    await r.aclose()
    print("Done! Results saved to stress_test_results.txt")


if __name__ == "__main__":
    asyncio.run(stress_test())
