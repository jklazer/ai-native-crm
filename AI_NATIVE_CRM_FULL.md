# AI-Native CRM: Генеративный стейт + мульти-агентная система

**Инженерный документ v1.0**
**Дата:** 2026-03-18
**Команда:** 2 человека (full-stack dev + product visionary)
**Контекст:** LLM как Decision Engine. Bitrix24 = source of truth. Стейт агента = генеративный JSON-кэш.

---

## Оглавление

| # | Раздел | Файл | Строк |
|---|--------|------|-------|
| 1 | [Архитектура генеративного стейта](#часть-1) | `part1_architecture.md` | 915 |
| 2 | [Proof-of-Concept: код + документация](#часть-2) | `poc.py` + `part2_poc.md` | 502 + 233 |
| 3 | [Сравнение: классика vs AI-native](#часть-3) | `part3_comparison.md` | 1088 |
| 4 | [Честные ограничения](#часть-4) | `part4_limitations.md` | 919 |
| — | **Итого** | **5 файлов** | **3657 строк** |

---

## Часть 1: Архитектура генеративного стейта {#часть-1}

> Файл: [`part1_architecture.md`](./part1_architecture.md)

**Содержание:**
- JSON-схема стейта с разделением hot data / cold summary
- Token budget: ≤5500 токенов на стейт (из 8000 окна)
- Anchor points: deal_id / contact_id только из Bitrix, никогда не генерируются LLM
- Lifecycle (Mermaid): Initialization → Evolution → Compression → Recovery
- Полный system prompt для CRM-агента с форматами input/output
- Правила компрессии и примеры

---

## Часть 2: Proof-of-Concept {#часть-2}

> Код: [`poc.py`](./poc.py) (502 строки)
> Документация: [`part2_poc.md`](./part2_poc.md)

**Что реализовано:**
- Эволюционный стейт (dataclass → JSON persistence)
- Telegram вход (aiogram 3.x, polling)
- Bitrix24 REST API (aiohttp) — pull deals, update deals
- Основной цикл: `стейт_N → LLM(стейт + input) → стейт_N+1 + actions`
- Action executor: send_telegram, update_bitrix, schedule_reminder
- Компрессия через tiktoken при превышении лимита
- LLM fallback: gpt-4o-mini → Claude Haiku

**Запуск:**
```bash
pip install aiogram==3.13.1 aiohttp tiktoken openai anthropic
export TELEGRAM_TOKEN="..." BITRIX_WEBHOOK="..." OPENAI_API_KEY="..."
python poc.py
```

---

## Часть 3: Сравнение классика vs AI-native {#часть-3}

> Файл: [`part3_comparison.md`](./part3_comparison.md)

**Ключевые числа (сценарий "новый лид"):**

| Критерий | Классика | AI-native |
|---|---|---|
| Строки кода | ~520 | ~220 (2.4×) |
| Latency p50 | 45 мс | 1100 мс |
| Latency p99 | 180 мс | 4500 мс |
| $/1K событий/день | $99/мес | $222/мес (2.2×) |
| Time to market | Недели | Дни |

**Вердикт:** Гибридная архитектура — классика на hot path, LLM на enrichment.

---

## Часть 4: Честные ограничения {#часть-4}

> Файл: [`part4_limitations.md`](./part4_limitations.md)

**Матрица рисков:**

| Ограничение | Severity | P (на 1000 событий) |
|---|---|---|
| Галлюцинации | CRITICAL | 20-40 инцидентов |
| Дрейф стейта | HIGH | Точность ↓59% после 4 compaction |
| Отсутствие ACID | CRITICAL | Race condition гарантирован |
| Стоимость inference | MEDIUM | $2/день при 500 событий (gpt-4o-mini) |
| Latency | HIGH | 300-2000 мс vs 1-50 мс |
| 152-ФЗ | CRITICAL | Штрафы до 6 млн руб. |

**Главный вывод:** Чистый AI-native без grounding — архитектурная ошибка. Правильный ответ — гибрид: классика для фактов, AI для смысла.

---

## Заключение и следующие шаги

### Что мы получили

1. **Архитектура** — полная JSON-схема генеративного стейта с lifecycle и system prompt
2. **Рабочий PoC** — 502 строки Python, запускается одной командой
3. **Честное сравнение** — AI-native быстрее в разработке (2.4× меньше кода), но медленнее и дороже в runtime
4. **Карта рисков** — 6 ограничений с конкретными mitigation strategies

### Roadmap на 4 недели

**Неделя 1: Валидация PoC**
- Развернуть poc.py с тестовым Bitrix24
- Прогнать 100 событий, замерить дрейф стейта
- Добавить grounding: числа и ID только из Bitrix, не из стейта

**Неделя 2: Безопасность данных**
- Анонимизация ФИО/телефонов перед отправкой в OpenAI (152-ФЗ)
- Redis distributed lock для ACID-подобных гарантий
- Иммутабельный список критических фактов (не сжимаются при compaction)

**Неделя 3: Гибридная архитектура**
- PostgreSQL для фактов (ID, суммы, статусы, даты)
- LLM-стейт только для семантики (приоритеты, контекст, план действий)
- Bitrix24 webhook → FastAPI → PostgreSQL + LLM enrichment

**Неделя 4: Нагрузочное тестирование**
- 1000 событий/день, замер стоимости и latency
- Decision framework: какие запросы идут через LLM, какие через прямой SQL
- Go/no-go решение по production deployment
