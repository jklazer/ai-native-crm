# Contributing to AI-Native CRM

## Принципы

1. **Нет SQL. Нет PostgreSQL.** Это ключевое архитектурное решение. Не добавляйте ORM, миграции, SQL-запросы.
2. **Redis — единственный persistence.** Все данные — в Redis с AOF.
3. **CRM API = source of truth.** Данные о сделках берутся из CRM, не из кеша.
4. **Антигаллюцинация обязательна.** Каждый deal_id и сумма валидируются через CRM.

## Как внести изменения

1. Fork → branch → PR
2. Пройдите все тесты: `pytest -v`
3. Новый код — с type hints
4. Новый CRM-адаптер — реализуйте `CRMAdapter` ABC

## Структура проекта

```
ai_native_crm/
├── adapters/    # CRM-адаптеры (Bitrix, AmoCRM, Mock)
├── core/        # Ядро: Engine, StateStore, Compressor, DriftDetector, Validator
├── services/    # Сервисы: LLM, PII, Lock, Metrics
└── tests/       # Тесты (fakeredis)
```

## Как добавить CRM-адаптер

1. Создайте файл в `adapters/your_crm.py`
2. Реализуйте все методы `CRMAdapter`
3. Добавьте в factory `adapters/__init__.py`
4. Добавьте настройки в `config.py`
5. Напишите тесты
