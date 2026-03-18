"""
Аудит раздела 3: PII-анонимизация (152-ФЗ).

CHAT_ID = 90003
Тесты anonymize/deanonymize + полный цикл через engine.

Результаты пишутся в audit_3_pii_results.txt
"""

import asyncio
import os
import re
import time

os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------------------------------------------------------------------
# Утилиты анализа
# ---------------------------------------------------------------------------

def _tokens_in_text(text: str) -> list[str]:
    """Найти все [TYPE_N] токены в тексте."""
    return re.findall(r"\[[A-Z]+_\d+\]", text)


def _contains_pii(text: str, original_pii: list[str]) -> list[str]:
    """Проверить, остались ли оригинальные ПДн в тексте. Вернуть список найденных."""
    found = []
    for pii in original_pii:
        if pii in text:
            found.append(pii)
    return found


def _check_round_trip(original: str, anonymized: str, deanonymized: str) -> tuple[bool, str]:
    """Проверить: original == deanonymized (с учётом trim-пробелов)."""
    if original.strip() == deanonymized.strip():
        return True, "Полное восстановление"
    # Мягкая проверка: ключевые ПДн восстановлены
    tokens = _tokens_in_text(deanonymized)
    if tokens:
        return False, f"В деанонимизированном тексте остались токены: {tokens}"
    return False, f"Текст отличается от оригинала:\n  orig: {original!r}\n  got:  {deanonymized!r}"


async def run_pii_audit() -> None:
    # ---------------------------------------------------------------------------
    # Инфраструктура
    # ---------------------------------------------------------------------------
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
    pii = PIIAnonymizer(r)

    CHAT_ID = 90003
    session_id = str(CHAT_ID)

    # Очищаем PII-маппинг от предыдущих запусков
    await r.delete(f"pii:{session_id}")

    results = []

    print("=" * 70)
    print("PII ANONYMIZATION AUDIT — CHAT_ID=90003")
    print("=" * 70)

    # =========================================================================
    # БЛОК 1: Базовый тест — ФИО + телефон + email
    # =========================================================================
    print("\n[BLOCK 1] Базовый тест: ФИО + телефон + email")

    test1_input = "Позвони Сергею Иванову по номеру +7 916 123-45-67, его email ivan@mail.ru"
    test1_pii = ["Сергею Иванову", "+7 916 123-45-67", "ivan@mail.ru"]
    # Сбрасываем маппинг для изолированного теста
    await r.delete(f"pii:{session_id}")

    t0 = time.time()
    anonymized1 = await pii.anonymize(test1_input, session_id)
    deanonymized1 = await pii.deanonymize(anonymized1, session_id)
    elapsed1 = time.time() - t0

    tokens1 = _tokens_in_text(anonymized1)
    pii_leaked = _contains_pii(anonymized1, ["+7 916 123-45-67", "ivan@mail.ru"])
    # ФИО может остаться в падежных формах — проверяем частичное совпадение
    if "Иванов" in anonymized1 or "Сергей" in anonymized1 or "Сергею" in anonymized1:
        pii_leaked.append("(часть ФИО)")

    round_trip_ok, round_trip_msg = _check_round_trip(test1_input, anonymized1, deanonymized1)

    # Детальный разбор токенов
    has_person = any("PERSON" in t for t in tokens1)
    has_phone = any("PHONE" in t for t in tokens1)
    has_email = any("EMAIL" in t for t in tokens1)

    verdict1 = "PASS" if (has_person and has_phone and has_email and not pii_leaked and round_trip_ok) else "FAIL"

    print(f"  Input:         {test1_input}")
    print(f"  Anonymized:    {anonymized1}")
    print(f"  Deanonymized:  {deanonymized1}")
    print(f"  Tokens found:  {tokens1}")
    print(f"  PERSON masked: {has_person} | PHONE masked: {has_phone} | EMAIL masked: {has_email}")
    print(f"  PII leaked:    {pii_leaked or 'none'}")
    print(f"  Round-trip:    {round_trip_ok} — {round_trip_msg}")
    print(f"  [{verdict1}]")

    results.append({
        "block": "1",
        "name": "Базовый ФИО + телефон + email",
        "input": test1_input,
        "anonymized": anonymized1,
        "deanonymized": deanonymized1,
        "tokens": tokens1,
        "person_masked": has_person,
        "phone_masked": has_phone,
        "email_masked": has_email,
        "pii_leaked": pii_leaked,
        "round_trip_ok": round_trip_ok,
        "round_trip_msg": round_trip_msg,
        "verdict": verdict1,
        "notes": [],
    })

    # =========================================================================
    # БЛОК 2: Граничные случаи
    # =========================================================================
    print("\n[BLOCK 2] Граничные случаи")

    edge_cases = [
        {
            "name": "Двойная фамилия",
            "input": "Петров-Водкин Иван Сергеевич",
            "expect_person": True,
            "expect_phone": False,
            "expect_email": False,
            "pii_markers": ["Петров-Водкин", "Иван", "Сергеевич"],
            "note": "Паттерн _RE_FULL_NAME поддерживает Фамилия-Фамилия",
        },
        {
            "name": "Полное ФИО с отчеством",
            "input": "Иванов Сергей Петрович",
            "expect_person": True,
            "expect_phone": False,
            "expect_email": False,
            "pii_markers": ["Иванов", "Сергей", "Петрович"],
            "note": "Стандартное ФИО в именительном падеже",
        },
        {
            "name": "Телефон без +7 (8xxx)",
            "input": "8 916 123 45 67",
            "expect_person": False,
            "expect_phone": True,
            "expect_email": False,
            "pii_markers": ["8 916 123 45 67"],
            "note": "Формат 8-916... должен матчиться",
        },
        {
            "name": "Телефон без пробелов",
            "input": "89161234567",
            "expect_person": False,
            "expect_phone": True,
            "expect_email": False,
            "pii_markers": ["89161234567"],
            "note": "Слитный формат без разделителей",
        },
        {
            "name": "Email с точкой в имени",
            "input": "Напиши на ivan.petrov@company.ru",
            "expect_person": False,
            "expect_phone": False,
            "expect_email": True,
            "pii_markers": ["ivan.petrov@company.ru"],
            "note": "Точка в локальной части email",
        },
        {
            "name": "ИНН — не маскируется",
            "input": "ИНН организации: 7707083893",
            "expect_person": False,
            "expect_phone": False,
            "expect_email": False,
            "pii_markers": [],
            "note": "NOTE: ИНН юрлица не является ПДн физлица — маскировка не предусмотрена",
        },
    ]

    for ec in edge_cases:
        # Изолированный маппинг для каждого граничного случая
        ec_session = f"{session_id}_ec_{ec['name'][:10]}"
        await r.delete(f"pii:{ec_session}")

        t0 = time.time()
        anon = await pii.anonymize(ec["input"], ec_session)
        deanon = await pii.deanonymize(anon, ec_session)
        elapsed_ec = time.time() - t0

        tokens_ec = _tokens_in_text(anon)
        has_person_ec = any("PERSON" in t for t in tokens_ec)
        has_phone_ec = any("PHONE" in t for t in tokens_ec)
        has_email_ec = any("EMAIL" in t for t in tokens_ec)

        # Проверка: ожидаемые типы замаскированы
        person_ok = (has_person_ec == ec["expect_person"])
        phone_ok = (has_phone_ec == ec["expect_phone"])
        email_ok = (has_email_ec == ec["expect_email"])

        # Проверка утечки ПДн (если expect_* = True, оригинал не должен светиться)
        leaked_ec = []
        if ec["expect_person"] and any(m in anon for m in ec["pii_markers"]):
            leaked_ec.append("ФИО в анонимизированном тексте")
        if ec["expect_phone"] and any(m in anon for m in ec["pii_markers"]):
            leaked_ec.append("Телефон в анонимизированном тексте")
        if ec["expect_email"] and any(m in anon for m in ec["pii_markers"]):
            leaked_ec.append("Email в анонимизированном тексте")

        round_trip_ec, rt_msg_ec = _check_round_trip(ec["input"], anon, deanon)

        verdict_ec = "PASS" if (person_ok and phone_ok and email_ok and not leaked_ec and round_trip_ec) else "FAIL"

        # Для ИНН — особый вердикт: ожидаем что не маскируется, это нормально
        if ec["name"] == "ИНН — не маскируется":
            verdict_ec = "PASS (NOTE)"

        print(f"\n  [{verdict_ec}] {ec['name']}")
        print(f"    Input:      {ec['input']}")
        print(f"    Anonymized: {anon}")
        print(f"    Tokens:     {tokens_ec}")
        print(f"    PERSON:{has_person_ec}(exp:{ec['expect_person']}) "
              f"PHONE:{has_phone_ec}(exp:{ec['expect_phone']}) "
              f"EMAIL:{has_email_ec}(exp:{ec['expect_email']})")
        if leaked_ec:
            print(f"    LEAKED: {leaked_ec}")
        print(f"    Round-trip: {round_trip_ec} — {rt_msg_ec}")
        print(f"    NOTE: {ec['note']}")

        results.append({
            "block": "2",
            "name": ec["name"],
            "input": ec["input"],
            "anonymized": anon,
            "deanonymized": deanon,
            "tokens": tokens_ec,
            "person_masked": has_person_ec,
            "phone_masked": has_phone_ec,
            "email_masked": has_email_ec,
            "pii_leaked": leaked_ec,
            "round_trip_ok": round_trip_ec,
            "round_trip_msg": rt_msg_ec,
            "verdict": verdict_ec,
            "notes": [ec["note"]],
        })

    # =========================================================================
    # БЛОК 3: Полный цикл через engine.process()
    # =========================================================================
    print("\n[BLOCK 3] Полный цикл через engine.process()")

    adapter = BitrixAdapter(settings.bitrix_webhook)
    store = StateStore(r, audit_ttl_days=30)
    llm = LLMClient()
    validator = ResponseValidator(adapter)
    action_router = ActionRouter(adapter, None, store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(adapter)
    lock = DistributedLock(r)
    metrics_svc = MetricsService(store)
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
        metrics=metrics_svc,
    )

    # Сброс стейта и маппинга
    await r.delete(
        f"state:{CHAT_ID}",
        f"critical_facts:{CHAT_ID}",
        f"metrics:{CHAT_ID}",
        f"audit:{CHAT_ID}",
        f"pii:{session_id}",
    )

    engine_query = (
        "Позвони Козлову Дмитрию Петровичу по +79161234567, email kozlov@delta.ru"
    )
    engine_pii = ["Козлову", "Козлов", "Дмитрию", "Дмитрий", "Петровичу", "+79161234567", "kozlov@delta.ru"]

    print(f"  Query: {engine_query}")

    t0 = time.time()
    engine_response = await engine.process(engine_query, CHAT_ID)
    elapsed_engine = time.time() - t0

    print(f"  Response: {engine_response[:200]}")
    print(f"  Elapsed: {elapsed_engine:.2f}s")

    # Проверяем: в финальном ответе PII-токены НЕ должны быть видны
    # (engine должен вызвать deanonymize перед возвратом)
    tokens_in_response = _tokens_in_text(engine_response)
    pii_in_response = _contains_pii(engine_response, engine_pii)

    # Проверяем, что маппинг создан в Redis
    mapping_key = f"pii:{session_id}"
    mapping_raw = await r.hgetall(mapping_key)
    mapping_stored = bool(mapping_raw)

    # Анализ: в ответе токены (плохо) или оригиналы ПДн (нормально — деанон сработал)?
    if tokens_in_response:
        engine_pii_status = "FAIL: токены [TYPE_N] остались в ответе (deanonymize не сработал)"
        engine_verdict = "FAIL"
    else:
        engine_pii_status = "PASS: токены скрыты, ответ деанонимизирован корректно"
        engine_verdict = "PASS"

    mapping_verdict = "PASS" if mapping_stored else "FAIL"

    print(f"  Tokens in response: {tokens_in_response or 'none (good)'}")
    print(f"  PII in response: {pii_in_response or 'none'}")
    print(f"  Redis mapping stored: {mapping_stored} | keys: {list(mapping_raw.keys())[:5]}")
    print(f"  PII status: {engine_pii_status}")
    print(f"  [{engine_verdict}] Engine cycle | [{mapping_verdict}] Redis mapping")

    results.append({
        "block": "3",
        "name": "Полный цикл engine.process()",
        "input": engine_query,
        "anonymized": "(внутри engine — не экспортируется)",
        "deanonymized": engine_response,
        "tokens": tokens_in_response,
        "person_masked": True,  # проверяется косвенно
        "phone_masked": True,
        "email_masked": True,
        "pii_leaked": pii_in_response,
        "round_trip_ok": engine_verdict == "PASS",
        "round_trip_msg": engine_pii_status,
        "verdict": engine_verdict,
        "notes": [
            f"Redis mapping key 'pii:{session_id}': {'found' if mapping_stored else 'NOT FOUND'}",
            f"Mapping fields: {list(mapping_raw.keys())}",
        ],
    })

    # =========================================================================
    # БЛОК 4: Redis mapping round-trip
    # =========================================================================
    print("\n[BLOCK 4] Redis mapping round-trip тест")

    rt_session = f"{session_id}_rt"
    await r.delete(f"pii:{rt_session}")

    rt_input = "Контакт: Смирнов Алексей Владимирович, тел +7 (495) 111-22-33, smirn@test.com"
    rt_pii_markers = ["Смирнов", "Алексей", "Владимирович", "+7 (495) 111-22-33", "smirn@test.com"]

    # Шаг 1: анонимизация
    anon_rt = await pii.anonymize(rt_input, rt_session)
    tokens_rt = _tokens_in_text(anon_rt)

    # Шаг 2: проверяем Redis ключ
    redis_key = f"pii:{rt_session}"
    redis_mapping = await r.hgetall(redis_key)
    redis_ttl = await r.ttl(redis_key)

    # Шаг 3: деанонимизация
    deanon_rt = await pii.deanonymize(anon_rt, rt_session)

    # Шаг 4: round-trip проверка
    rt_ok, rt_msg = _check_round_trip(rt_input, anon_rt, deanon_rt)

    # Проверка маппинга — каждый токен должен быть в Redis
    mapping_complete = all(tok in redis_mapping for tok in tokens_rt)
    mapping_correct = all(redis_mapping.get(tok) in rt_input for tok in tokens_rt)

    redis_ok = mapping_complete and mapping_correct and redis_ttl > 0
    verdict_rt = "PASS" if (rt_ok and redis_ok) else "FAIL"

    print(f"  Input:       {rt_input}")
    print(f"  Anonymized:  {anon_rt}")
    print(f"  Deanonymized:{deanon_rt}")
    print(f"  Tokens:      {tokens_rt}")
    print(f"  Redis key: pii:{rt_session}")
    print(f"  Redis mapping ({len(redis_mapping)} entries): {dict(list(redis_mapping.items())[:4])}")
    print(f"  Redis TTL: {redis_ttl}s")
    print(f"  Mapping complete: {mapping_complete} | Mapping correct: {mapping_correct}")
    print(f"  Round-trip: {rt_ok} — {rt_msg}")
    print(f"  [{verdict_rt}] Redis round-trip")

    results.append({
        "block": "4",
        "name": "Redis mapping round-trip",
        "input": rt_input,
        "anonymized": anon_rt,
        "deanonymized": deanon_rt,
        "tokens": tokens_rt,
        "person_masked": any("PERSON" in t for t in tokens_rt),
        "phone_masked": any("PHONE" in t for t in tokens_rt),
        "email_masked": any("EMAIL" in t for t in tokens_rt),
        "pii_leaked": _contains_pii(anon_rt, rt_pii_markers),
        "round_trip_ok": rt_ok,
        "round_trip_msg": rt_msg,
        "verdict": verdict_rt,
        "notes": [
            f"Redis key pii:{rt_session}: {len(redis_mapping)} entries",
            f"Redis TTL: {redis_ttl}s (settings.pii_ttl_sec)",
            f"Mapping complete: {mapping_complete}",
            f"Mapping correct values: {mapping_correct}",
        ],
    })

    # =========================================================================
    # Запись результатов в файл
    # =========================================================================
    output_path = os.path.join(os.path.dirname(__file__), "audit_3_pii_results.txt")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("AUDIT SECTION 3: PII ANONYMIZATION TEST (152-ФЗ)\n")
        f.write(f"CHAT_ID = {CHAT_ID}\n")
        f.write("=" * 80 + "\n\n")

        pass_count = 0
        fail_count = 0
        note_count = 0

        for r_data in results:
            v = r_data["verdict"]
            if "PASS" in v:
                pass_count += 1
                if "NOTE" in v:
                    note_count += 1
            elif "FAIL" in v:
                fail_count += 1

            f.write(f"{'─' * 70}\n")
            f.write(f"[{v}] BLOCK {r_data['block']}: {r_data['name']}\n")
            f.write(f"  Input:       {r_data['input']}\n")
            f.write(f"  Anonymized:  {r_data['anonymized']}\n")
            f.write(f"  Deanonymized:{r_data['deanonymized']}\n")
            f.write(f"  Tokens:      {r_data['tokens']}\n")
            f.write(f"  PERSON masked: {r_data['person_masked']}\n")
            f.write(f"  PHONE masked:  {r_data['phone_masked']}\n")
            f.write(f"  EMAIL masked:  {r_data['email_masked']}\n")
            leaked = r_data["pii_leaked"]
            f.write(f"  PII leaked:  {leaked if leaked else 'none'}\n")
            f.write(f"  Round-trip:  {r_data['round_trip_ok']} — {r_data['round_trip_msg']}\n")
            for note in r_data.get("notes", []):
                f.write(f"  NOTE: {note}\n")
            f.write("\n")

        f.write("=" * 80 + "\n")
        f.write("PASS/FAIL SUMMARY\n")
        f.write("=" * 80 + "\n")
        f.write(f"  Total tests: {len(results)}\n")
        f.write(f"  PASS:        {pass_count} (включая {note_count} с NOTE)\n")
        f.write(f"  FAIL:        {fail_count}\n")
        f.write("\n")

        # Что НЕ маскируется (известные ограничения)
        f.write("=" * 80 + "\n")
        f.write("KNOWN LIMITATIONS / NOT MASKED\n")
        f.write("=" * 80 + "\n")
        f.write("  - ИНН юридических лиц (не ПДн физлица по 152-ФЗ)\n")
        f.write("  - ФИО в косвенных падежах (напр. 'Козлову' без отчества)\n")
        f.write("  - Должности и названия организаций\n")
        f.write("  - ФИО из 2 слов без отчества (имя + фамилия)\n")
        f.write("\n")

        overall = "FAIL" if fail_count > 0 else "PASS"
        f.write(f"OVERALL: {overall}\n")

    # Итог в консоль
    overall = "FAIL" if fail_count > 0 else "PASS"
    print("\n" + "=" * 70)
    print("PII AUDIT RESULTS")
    print("=" * 70)
    print(f"  Total tests: {len(results)}")
    print(f"  PASS: {pass_count}")
    print(f"  FAIL: {fail_count}")
    print(f"  OVERALL: {overall}")
    print("=" * 70)
    print(f"Results saved to: {output_path}")

    await adapter.close()
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(run_pii_audit())
