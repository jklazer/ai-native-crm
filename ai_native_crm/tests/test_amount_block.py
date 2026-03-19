"""
Тест блокировки действия при AMOUNT_MISMATCH.

Проверяет, что validate() удаляет action из списка, если предложенная
LLM сумма расходится с суммой в CRM, и добавляет AMOUNT_MISMATCH-алерт.
"""

import pytest

from ai_native_crm.adapters.mock import MockAdapter
from ai_native_crm.adapters.base import DealInfo
from ai_native_crm.core.response_validator import ResponseValidator


def _make_adapter_with_deal(deal_id: str, amount: float) -> MockAdapter:
    """
    Создать MockAdapter и добавить сделку с заданной суммой.

    MockAdapter при инициализации наполняется seed-сделками d1–d5.
    Мы добавляем тестовую сделку поверх, чтобы не зависеть от seed-данных.
    """
    adapter = MockAdapter()
    adapter._deals[deal_id] = DealInfo(
        id=deal_id,
        title="Тестовая сделка",
        stage="NEW",
        amount=amount,
    )
    return adapter


async def test_amount_mismatch_blocks_action():
    """
    Если LLM предлагает update_deal с суммой, отличной от CRM,
    action должен быть удалён из результата, а список alerts
    должен содержать запись AMOUNT_MISMATCH.
    """
    deal_id = "d42"
    crm_amount = 340_000.0
    proposed_amount = 500_000.0

    adapter = _make_adapter_with_deal(deal_id, crm_amount)
    validator = ResponseValidator(adapter)

    response = {
        "response": f"Обновил сумму сделки {deal_id} до {proposed_amount}.",
        "actions": [
            {
                "type": "update_deal",
                "target": "crm",
                "params": {
                    "deal_id": deal_id,
                    "fields": {
                        "OPPORTUNITY": proposed_amount,
                    },
                },
            }
        ],
    }

    deals = await adapter.get_deals()
    fixed, alerts = await validator.validate(response, deals)

    # Action должен быть удалён из результата
    assert len(fixed["actions"]) == 0, (
        f"Ожидался пустой список actions, получен: {fixed['actions']}"
    )

    # Должен присутствовать ровно один AMOUNT_MISMATCH-алерт для нашей сделки
    amount_alerts = [a for a in alerts if "AMOUNT_MISMATCH" in a and deal_id in a]
    assert len(amount_alerts) == 1, (
        f"Ожидался один AMOUNT_MISMATCH-алерт, получено {len(amount_alerts)}: {alerts}"
    )
