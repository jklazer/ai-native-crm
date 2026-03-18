"""
Тесты StateCompressor — 2 теста.
Используем FakeLLM для суммаризации; никакого реального LLM.
"""
import pytest

from ai_native_crm.config import settings
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.state_store import SemanticState


# ---------------------------------------------------------------------------
# Заглушка LLM
# ---------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, resp: dict):
        self._resp = resp

    async def call(self, messages: list[dict]) -> tuple[dict, dict]:
        return self._resp, {
            "model": "fake",
            "tokens_in": 10,
            "tokens_out": 20,
            "latency_ms": 5,
        }


# ---------------------------------------------------------------------------
# test 1: длинный стейт → needs_compression = True
# ---------------------------------------------------------------------------


async def test_needs_compression_true():
    """
    Стейт с working_memory превышающей token_budget токенов
    должен требовать компрессию.
    """
    llm = FakeLLM({"working_memory": "сжато", "conversation_summary": "резюме"})
    compressor = StateCompressor(llm)

    # Создаём стейт с working_memory значительно превышающей token_budget.
    # Используем разнообразный текст (не повторяющийся) чтобы tiktoken не мёрджил токены.
    # token_budget = 3000, поэтому нужно ~3000+ уникальных токенов.
    # Обычный English/Russian текст даёт ~1 токен/слово.
    # Генерируем >3000 уникальных слов — каждое слово отдельный токен.
    words = [f"слово{i}" for i in range(settings.token_budget + 200)]
    long_memory = " ".join(words)

    state = SemanticState(
        chat_id=1,
        iteration=5,
        working_memory=long_memory,
    )

    assert compressor.needs_compression(state) is True

    # После компрессии — working_memory должна стать короче лимита символов
    compressed_state = await compressor.compress(state)
    assert len(compressed_state.working_memory) <= settings.wm_max_chars


# ---------------------------------------------------------------------------
# test 2: короткий стейт → needs_compression = False
# ---------------------------------------------------------------------------


async def test_short_no_compression():
    """
    Стейт с маленькой working_memory не должен требовать компрессию.
    compress() при этом возвращает стейт без изменений.
    """
    llm = FakeLLM({"working_memory": "не должно вызваться", "conversation_summary": ""})
    compressor = StateCompressor(llm)

    short_memory = "Короткая рабочая память"  # явно меньше wm_max_chars
    state = SemanticState(
        chat_id=2,
        iteration=1,
        working_memory=short_memory,
    )

    assert compressor.needs_compression(state) is False

    # compress() не должен вызывать LLM, возвращает тот же стейт
    unchanged = await compressor.compress(state)
    assert unchanged.working_memory == short_memory
