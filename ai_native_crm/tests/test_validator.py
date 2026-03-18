"""
Тесты ResponseValidator — 3 теста.
MockAdapter как источник «правды» о сделках; никакого SQL.
"""
import pytest

from ai_native_crm.adapters.mock import MockAdapter
from ai_native_crm.core.response_validator import ResponseValidator


def _make_validator(adapter=None) -> ResponseValidator:
    if adapter is None:
        adapter = MockAdapter()
    return ResponseValidator(adapter)


# ---------------------------------------------------------------------------
# test 1: валидный ответ проходит без изменений и без алертов
# ---------------------------------------------------------------------------


async def test_valid_passes():
    """
    Ответ со ссылкой на существующий deal_id и верной суммой —
    проходит без алертов, actions не изменяются.
    """
    adapter = MockAdapter()
    validator = _make_validator(adapter)

    # d1 существует в MockAdapter
    response = {
        "response": "Сделка d1 в стадии PREPARATION.",
        "actions": [
            {
                "type": "update_deal",
                "target": "crm",
                "params": {
                    "deal_id": "d1",
                    "fields": {"stage": "NEGOTIATION"},
                },
            }
        ],
    }

    # Получаем актуальные сделки (как делает engine)
    deals = await adapter.get_deals()
    fixed, alerts = await validator.validate(response, deals)

    assert alerts == []
    assert len(fixed["actions"]) == 1
    assert fixed["actions"][0]["params"]["deal_id"] == "d1"


# ---------------------------------------------------------------------------
# test 2: несуществующий deal_id → action удаляется, alert добавляется
# ---------------------------------------------------------------------------


async def test_invalid_deal_removed():
    """
    Если action ссылается на deal_id которого нет в CRM,
    action должен быть удалён, а в alerts появится запись о галлюцинации.
    """
    adapter = MockAdapter()
    validator = _make_validator(adapter)

    response = {
        "response": "Обновил сделку d999.",
        "actions": [
            {
                "type": "update_deal",
                "target": "crm",
                "params": {
                    "deal_id": "d999",  # не существует
                    "fields": {"stage": "WON"},
                },
            }
        ],
    }

    deals = await adapter.get_deals()
    fixed, alerts = await validator.validate(response, deals)

    # Action удалён
    assert len(fixed["actions"]) == 0

    # В alerts — хотя бы одна запись о несуществующей сделке d999
    # (валидатор может генерировать несколько алертов: от action и от текста)
    assert len(alerts) >= 1
    hallucination_alerts = [a for a in alerts if "HALLUCINATION" in a and "d999" in a]
    assert len(hallucination_alerts) >= 1


# ---------------------------------------------------------------------------
# test 3: несоответствие суммы → alert добавляется, action остаётся
# ---------------------------------------------------------------------------


async def test_amount_mismatch():
    """
    Если proposed_amount в update_deal расходится с суммой в CRM,
    alert должен быть добавлен, но action НЕ удаляется
    (менеджер может законно менять сумму).
    """
    adapter = MockAdapter()
    validator = _make_validator(adapter)

    # d1 имеет amount=450_000 в MockAdapter
    response = {
        "response": "Обновил сумму сделки d1 на 999999.",
        "actions": [
            {
                "type": "update_deal",
                "target": "crm",
                "params": {
                    "deal_id": "d1",
                    "fields": {
                        "stage": "NEGOTIATION",
                        "OPPORTUNITY": 999_999.0,  # существенно отличается от 450_000
                    },
                },
            }
        ],
    }

    deals = await adapter.get_deals()
    fixed, alerts = await validator.validate(response, deals)

    # Action остаётся (validator не блокирует обновление суммы)
    assert len(fixed["actions"]) == 1

    # Но alert о несоответствии суммы должен быть зафиксирован
    amount_alerts = [a for a in alerts if "AMOUNT_MISMATCH" in a]
    assert len(amount_alerts) == 1
    assert "d1" in amount_alerts[0]
