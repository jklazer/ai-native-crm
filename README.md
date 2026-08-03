# AI-Native CRM Agent — Zero-DB Architecture

> AI-агент для CRM без базы данных. Redis — единственный persistence. CRM API — единственный source of truth.

## Ключевая идея

Вместо классической архитектуры "LLM + PostgreSQL + ORM + миграции" — семантический стейт,
который LLM перезаписывает каждый ход. Только Redis AOF и CRM REST API. Ноль SQL.

## Чем отличается от аналогов

| | Letta/MemGPT | LangChain Agents | CrewAI | **AI-Native CRM** |
|---|---|---|---|---|
| Database | PostgreSQL + pgvector | PostgreSQL/SQLite | LanceDB | **Нет. Только Redis** |
| Deploy time | 10+ мин | Зависит | 5+ мин | **30 сек** |
| CRM integration | Нет | Нет | Нет | **Bitrix24, AmoCRM** |
| Anti-hallucination | Нет | Нет | Нет | **Встроенный валидатор** |
| PII (152-ФЗ) | Нет | Нет | Нет | **Из коробки** |
| State compression | Вручную | Нет | Нет | **Автоматическая** |

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/jklazer/ai-native-crm.git
cd ai-native-crm

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Настроить
cp .env.example .env
# Заполнить: TELEGRAM_TOKEN, OPENAI_API_KEY, BITRIX_WEBHOOK (или AMO_*)

# 4. Запустить Redis
docker run -d --name redis -p 6379:6379 redis:7-alpine

# 5. Запустить бота
python -m ai_native_crm.main
```

## Архитектура

```
Telegram → AgentEngine (10-step pipeline) → Response
                ↓              ↓
          CRM API          Redis (AOF)
       (source of truth)   (semantic state)
```

### 10-шаговый pipeline

1. **Lock** — distributed lock на chat_id (Redis SET NX PX)
2. **Load State** — загрузка SemanticState из Redis
3. **CRM Deals** — актуальные сделки из CRM API (source of truth)
4. **Critical Facts** — бизнес-факты из Redis (append-only)
5. **PII Anonymize** — маскирование ПДн перед LLM (152-ФЗ)
6. **LLM Call** — GPT-4o-mini (primary) + Claude Haiku (fallback)
7. **Validate** — проверка галлюцинаций (deal_id, суммы)
8. **Actions** — выполнение команд (CRM update, reminders)
9. **Save State** — запись нового стейта в Redis
10. **Audit** — логирование в Redis Stream + метрики

### Ключевые концепции

- **Semantic State**: JSON-объект перезаписываемый каждый ход. working_memory + assessment + summary.
- **Critical Facts**: Append-only список (Redis List). Бюджеты, отказы, дедлайны. Никогда не удаляются.
- **State Compression**: Когда стейт превышает token budget — LLM-суммаризация с сохранением deal_id.
- **Drift Detection**: Сравнение стейта агента с CRM API каждые N ходов.
- **Anti-Hallucination**: ResponseValidator проверяет каждый deal_id и сумму через CRM API.

## CRM-адаптеры

Поддерживаются:
- **Bitrix24** — через REST API webhook
- **AmoCRM** — через API v4 + OAuth2

### Подключить свою CRM

Реализуй `CRMAdapter` из `ai_native_crm/adapters/base.py`:

```python
class MyCRMAdapter(CRMAdapter):
    async def get_deals(self, filters=None) -> list[DealInfo]: ...
    async def update_deal(self, deal_id, fields) -> bool: ...
    async def create_deal(self, data) -> str: ...
    async def get_contacts(self, filters=None) -> list[ContactInfo]: ...
    async def verify_deal_exists(self, deal_id) -> bool: ...
    async def get_deal_amount(self, deal_id) -> float | None: ...
```

Добавь адаптер в `adapters/__init__.py` → factory `get_adapter()`.

## Переменные окружения

| Переменная | Обязательно | Описание |
|---|---|---|
| `TELEGRAM_TOKEN` | Да | Токен Telegram бота |
| `OPENAI_API_KEY` | Да | OpenAI API ключ |
| `REDIS_URL` | Нет | URL Redis (default: redis://localhost:6379/0) |
| `CRM_ADAPTER` | Нет | Адаптер: bitrix / amo / mock (default: mock) |
| `BITRIX_WEBHOOK` | При bitrix | Webhook URL Bitrix24 |
| `AMO_SUBDOMAIN` | При amo | Субдомен AmoCRM |
| `AMO_ACCESS_TOKEN` | При amo | OAuth2 access token |
| `PII_ENABLED` | Нет | Маскирование ПДн (default: true) |

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Начало работы |
| `/deals` | Текущие сделки из CRM |
| `/state` | Семантический стейт агента |
| `/facts` | Критические факты |
| `/metrics` | Метрики качества |
| `/drift` | Проверка синхронизации с CRM |

## Тесты

```bash
pytest -v                    # Все тесты (fakeredis, без внешних API)
python stress_test.py        # 30-turn stress test (нужен Redis + Bitrix)
python demo.py               # Демонстрация всех возможностей
```

## Лицензия

MIT — см. [LICENSE](LICENSE)
