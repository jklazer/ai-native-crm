# Part 2: Proof-of-Concept — AI-native CRM

## Архитектура кода

### Общая схема потока данных

```
Telegram сообщение
       │
       ▼
  handle_message()          ← aiogram dispatcher
       │
       ▼
  get_or_create_state()     ← load из файла или создать новый AgentState
       │
       ▼
  agent_turn()              ← ГЛАВНЫЙ ХОД АГЕНТА
  ├── compress_state()      ← компрессия если история > TOKEN_BUDGET токенов
  ├── build prompt          ← state_snapshot + history[-3] + user_input → messages[]
  ├── call_llm()            ← OpenAI gpt-4o-mini → fallback → Anthropic Haiku
  ├── execute_actions()     ← update_bitrix | schedule_reminder | refresh_bitrix
  ├── state.iteration += 1  ← эволюция стейта → state_N+1
  ├── state.history.append  ← запись хода в историю
  └── save_state()          ← JSON-файл на диск
       │
       ▼
  ответ пользователю в Telegram
```

### Компоненты

#### `AgentState` — эволюционный стейт
Dataclass, сериализуется в JSON-файл `crm_state.json` после каждого хода.

| Поле | Тип | Назначение |
|------|-----|------------|
| `chat_id` | int | Идентификатор Telegram-чата |
| `iteration` | int | Счётчик ходов (монотонно растёт) |
| `deals` | list[Deal] | Кэш сделок из Bitrix24 |
| `working_memory` | str | Рабочая память агента (генерируется LLM) |
| `reminders` | list[Reminder] | Запланированные напоминания |
| `history` | list[dict] | История диалога (сжимается при переполнении) |

#### LLM как Decision Engine
LLM получает **стейт + пользовательский ввод** и возвращает **структурированный JSON**:

```json
{
  "response": "текст ответа менеджеру",
  "actions": [
    {"type": "update_bitrix", "deal_id": "42", "fields": {"STAGE_ID": "NEGOTIATION"}},
    {"type": "schedule_reminder", "text": "Позвонить Иванову", "delay_seconds": 3600},
    {"type": "refresh_bitrix", "reason": "нужны свежие данные"}
  ],
  "new_working_memory": "Менеджер работает над сделкой #42, ключевой клиент — Иванов",
  "state_summary": "Переговоры по крупной сделке, ждём ответа клиента"
}
```

LLM = единственная точка принятия решений. Никакой if/else логики для выбора action.

#### Компрессия стейта
- Токены считаются через `tiktoken` (кодировка `cl100k_base`)
- История: если превышает `TOKEN_BUDGET` (3000 токенов) — оставляем последние 5 записей
- `working_memory`: обрезается до 2000 символов если переполнена
- В продакшне: компрессию `working_memory` тоже делает LLM (суммаризация)

#### Bitrix24 интеграция
- `bitrix_get_deals()` — список открытых сделок (REST: `crm.deal.list`)
- `bitrix_update_deal()` — обновление полей сделки (REST: `crm.deal.update`)
- Webhook-URL содержит токен авторизации (вшит в URL по стандарту Bitrix24)

#### Fallback между LLM
```
OPENAI_API_KEY задан → пробуем OpenAI gpt-4o-mini
         ↓ ошибка
ANTHROPIC_API_KEY задан → Anthropic Claude Haiku
         ↓ ошибка
Exception
```

#### Планировщик напоминаний
Фоновая `asyncio.Task`, проверяет `state.reminders` каждые 60 секунд. Просроченные напоминания отправляет в Telegram и удаляет из стейта.

---

## Как запустить

### 1. Установить зависимости

```bash
pip install aiogram==3.13.1 aiohttp==3.10.5 tiktoken==0.7.0 openai anthropic
```

Или создать `requirements.txt`:

```
aiogram==3.13.1
aiohttp==3.10.5
tiktoken==0.7.0
openai>=1.0.0
anthropic>=0.25.0
```

```bash
pip install -r requirements.txt
```

### 2. Настроить переменные окружения

```bash
# Windows (PowerShell)
$env:TELEGRAM_TOKEN    = "1234567890:ABCdef..."
$env:BITRIX_WEBHOOK    = "https://your-domain.bitrix24.ru/rest/1/your-token/"
$env:OPENAI_API_KEY    = "sk-..."
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # опционально, используется как fallback

# Linux / macOS
export TELEGRAM_TOKEN="1234567890:ABCdef..."
export BITRIX_WEBHOOK="https://your-domain.bitrix24.ru/rest/1/your-token/"
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

Минимальный набор: `TELEGRAM_TOKEN` + `BITRIX_WEBHOOK` + один из API-ключей LLM.

### 3. Запустить

```bash
python poc.py
```

Файл `crm_state.json` будет создан автоматически в текущей директории.

---

## Примеры взаимодействия

### Пример 1 — Просмотр сделок

```
Пользователь: /start
Агент: Инициализирую агента, загружаю данные из Bitrix24...
Агент: Готово! Загружено сделок: 7.
       Итерация стейта: 0
       Напишите любой вопрос или задачу...

Пользователь: Покажи топ сделки по сумме
Агент: Вот текущие топ-сделки из Bitrix24:

       1. "Поставка оборудования ООО Альфа" — 850 000 ₽, стадия: NEGOTIATION
       2. "Годовой контракт Бета Групп" — 420 000 ₽, стадия: PREPAYMENT_INVOICE
       3. "Консалтинг СМБ Гамма" — 180 000 ₽, стадия: NEW

       Всего открытых сделок: 7. Хотите обновить статус по какой-либо?
```

### Пример 2 — Обновление сделки

```
Пользователь: Переведи сделку #12 в стадию выставления счёта
Агент: Обновляю сделку #12 в Bitrix24...
       Готово! Сделка "Поставка оборудования ООО Альфа" переведена в стадию
       PREPAYMENT_INVOICE. Хотите установить напоминание для follow-up?
```

В этот момент action executor выполнил:
```json
{"type": "update_bitrix", "deal_id": "12", "fields": {"STAGE_ID": "PREPAYMENT_INVOICE"}}
```

### Пример 3 — Напоминание

```
Пользователь: Напомни мне позвонить Петрову по сделке #12 через 2 часа
Агент: Напоминание установлено. Через 2 часа я пришлю вам сообщение:
       "Позвонить Петрову по сделке #12 — Поставка оборудования ООО Альфа"

[через 2 часа]
Агент: Напоминание: Позвонить Петрову по сделке #12 — Поставка оборудования ООО Альфа
```

### Пример 4 — Контекстная рабочая память

```
Пользователь: Кстати, они просили скидку 10%
Агент: Понял, сохраняю в контекст сделки. Иванов из ООО Альфа запрашивает
       скидку 10% на поставку оборудования. Хотите обновить сумму сделки
       (850 000 → 765 000 ₽) или сначала согласуете с руководством?

[working_memory обновилась на:
"Работаем по сделке #12, ООО Альфа. Петров запросил скидку 10%.
 Текущая сумма 850 000 ₽, потенциальная после скидки — 765 000 ₽.
 Ждём согласования."]
```

---

## Стейт после 4 ходов (`crm_state.json`)

```json
{
  "chat_id": 123456789,
  "iteration": 4,
  "last_updated": "2025-09-15T14:32:11.453Z",
  "deals": [
    {"id": "12", "title": "Поставка оборудования ООО Альфа",
     "stage": "PREPAYMENT_INVOICE", "amount": 850000.0, "contact_name": "456"}
  ],
  "working_memory": "Работаем по сделке #12, ООО Альфа. Петров запросил скидку 10%...",
  "reminders": [],
  "history": [
    {"iteration": 1, "user": "Покажи топ сделки...", "agent": "Вот топ-сделки...", "actions": []},
    {"iteration": 2, "user": "Переведи сделку #12...", "agent": "Обновляю...", "actions": ["update_bitrix"]},
    {"iteration": 3, "user": "Напомни позвонить...", "agent": "Напоминание установлено...", "actions": ["schedule_reminder"]},
    {"iteration": 4, "user": "Кстати, они просили скидку 10%", "agent": "Понял...", "actions": []}
  ]
}
```

---

## Ограничения PoC и что добавить в продакшн

| PoC | Продакшн |
|-----|----------|
| Стейт в одном JSON-файле | Redis или PostgreSQL, разделение по chat_id |
| Компрессия обрезкой | Суммаризация через LLM (отдельный call) |
| Один пользователь | Multi-tenant, auth, rate limiting |
| Polling | Webhook с HTTPS |
| Синхронный планировщик | Celery / APScheduler |
| Нет валидации actions | JSON Schema валидация ответа LLM |
| Нет тестов | pytest + mock LLM responses |
