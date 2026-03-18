"""
Публичный API пакета core.

Реэкспорт всех ключевых классов CRM-движка для удобного импорта:
    from ai_native_crm.core import AgentEngine, ResponseValidator, ...
"""

from ai_native_crm.core.action_router import ActionResult, ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.core.engine import AgentEngine
from ai_native_crm.core.response_validator import ResponseValidator

__all__ = [
    "AgentEngine",
    "ResponseValidator",
    "ActionRouter",
    "ActionResult",
    "StateCompressor",
    "DriftDetector",
]
