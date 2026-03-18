from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_token: str

    # Redis — ЕДИНСТВЕННЫЙ storage, никакого SQL
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout: int = 10   # seconds — timeout on individual Redis operations
    redis_connect_timeout: int = 5   # seconds — timeout on initial TCP connection

    # LLM
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_fallback_model: str = "claude-haiku-4-5-20251001"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1024

    # CRM адаптер
    crm_adapter: str = "mock"  # bitrix | amo | mock
    bitrix_webhook: str = ""

    # AmoCRM
    amo_subdomain: str = ""
    amo_access_token: str = ""
    amo_refresh_token: str = ""
    amo_client_id: str = ""
    amo_client_secret: str = ""
    amo_redirect_uri: str = ""

    # Стейт — лимиты памяти
    token_budget: int = 3000
    wm_max_chars: int = 4000

    # Метрики — пороги go/no-go для мониторинга качества
    hallucination_threshold: float = 0.05
    drift_threshold: float = 0.40
    action_success_threshold: float = 0.90

    # Distributed lock — таймаут захвата
    lock_timeout_sec: int = 30

    # PII — маскирование персональных данных
    pii_enabled: bool = True
    pii_ttl_sec: int = 3600

    # Audit trail — срок хранения записей
    audit_ttl_days: int = 30

    # Проверка дрейфа стейта — каждые N ходов
    drift_check_interval: int = 10

    # Планировщик напоминаний — интервал опроса в секундах
    reminder_check_interval: int = 60

    # Веб-панель — API-ключ для авторизации
    web_api_key: str = "change-me-in-production"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
