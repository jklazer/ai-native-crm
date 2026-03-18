"""
CLI для быстрого старта AI-Native CRM.

Использование:
    ai-crm init      # Инициализация проекта
    ai-crm start     # Запуск бота
    ai-crm status    # Статус системы
    ai-crm reset     # Сброс стейта
    ai-crm web       # Запуск веб-панели
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="ai-crm",
        description="AI-Native CRM Agent — Zero-DB Architecture",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize project configuration")

    # start
    start_parser = subparsers.add_parser("start", help="Start the Telegram bot")
    start_parser.add_argument("--no-redis", action="store_true", help="Skip Redis check")

    # status
    status_parser = subparsers.add_parser("status", help="Show system status")
    status_parser.add_argument("--chat-id", type=int, help="Specific chat_id to inspect")

    # reset
    reset_parser = subparsers.add_parser("reset", help="Reset agent state")
    reset_parser.add_argument("--chat-id", type=int, required=True, help="Chat ID to reset")
    reset_parser.add_argument("--confirm", action="store_true", help="Skip confirmation")

    # web
    web_parser = subparsers.add_parser("web", help="Start web dashboard")
    web_parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "status":
        asyncio.run(cmd_status(args))
    elif args.command == "reset":
        asyncio.run(cmd_reset(args))
    elif args.command == "web":
        cmd_web(args)
    else:
        parser.print_help()


def cmd_init():
    """Interactive initialization — create .env with template."""
    print("AI-Native CRM — Initialization")
    print("=" * 40)

    if os.path.exists(".env"):
        overwrite = input(".env already exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("Skipped.")
            return

    # Ask questions
    print("\n1. CRM Adapter")
    adapter = input("   Choose: [bitrix] / amo / mock (default: mock): ").strip() or "mock"

    telegram_token = input("\n2. Telegram Bot Token: ").strip()
    openai_key = input("3. OpenAI API Key: ").strip()

    bitrix_webhook = ""
    amo_settings = {}

    if adapter == "bitrix":
        bitrix_webhook = input("4. Bitrix24 Webhook URL: ").strip()
    elif adapter == "amo":
        amo_settings["subdomain"] = input("4. AmoCRM subdomain: ").strip()
        amo_settings["access_token"] = input("5. AmoCRM access token: ").strip()

    redis_url = input(f"\n{'5' if adapter == 'mock' else '6'}. Redis URL (default: redis://localhost:6379/0): ").strip() or "redis://localhost:6379/0"

    # Write .env
    lines = [
        f"TELEGRAM_TOKEN={telegram_token}",
        f"OPENAI_API_KEY={openai_key}",
        f"ANTHROPIC_API_KEY=",
        f"REDIS_URL={redis_url}",
        f"CRM_ADAPTER={adapter}",
        f"BITRIX_WEBHOOK={bitrix_webhook}",
        f"PII_ENABLED=true",
    ]

    if adapter == "amo":
        lines.extend([
            f"AMO_SUBDOMAIN={amo_settings.get('subdomain', '')}",
            f"AMO_ACCESS_TOKEN={amo_settings.get('access_token', '')}",
            f"AMO_REFRESH_TOKEN=",
            f"AMO_CLIENT_ID=",
            f"AMO_CLIENT_SECRET=",
            f"AMO_REDIRECT_URI=",
        ])

    with open(".env", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n.env created with {adapter} adapter.")
    print("Run 'ai-crm start' to launch the bot.")


def cmd_start(args):
    """Start the Telegram bot."""
    # Check .env
    if not os.path.exists(".env"):
        print("Error: .env not found. Run 'ai-crm init' first.")
        sys.exit(1)

    # Check Redis
    if not args.no_redis:
        try:
            import redis
            from ai_native_crm.config import settings
            r = redis.from_url(settings.redis_url)
            r.ping()
            print("Redis: OK")
            r.close()
        except Exception as e:
            print(f"Redis not available: {e}")
            print("Start Redis: docker run -d --name redis -p 6379:6379 redis:7-alpine")
            print("Or use --no-redis to skip this check.")
            sys.exit(1)

    print("Starting AI-Native CRM bot...")
    # Import and run the bot
    # Use subprocess to ensure clean process
    os.execvp(sys.executable, [sys.executable, "-m", "ai_native_crm.main"])


async def cmd_status(args):
    """Show system status."""
    import redis.asyncio as aioredis
    from ai_native_crm.config import settings
    from ai_native_crm.core.state_store import StateStore

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    store = StateStore(r)

    try:
        # Redis ping
        await r.ping()
        print("Redis: Connected")
    except Exception:
        print("Redis: OFFLINE")
        return

    if args.chat_id:
        # Show specific chat
        state = await store.load(args.chat_id)
        facts = await store.get_critical_facts(args.chat_id)
        metrics = await store.get_metrics(args.chat_id)

        print(f"\nChat ID: {args.chat_id}")
        print(f"  Iteration:   {state.iteration}")
        print(f"  Last update: {state.last_updated or 'never'}")
        print(f"  WM size:     {len(state.working_memory)} chars")
        print(f"  Facts:       {len(facts)}")
        print(f"  Turns:       {metrics.get('total_turns', 0)}")
        print(f"  Hallucinations: {metrics.get('hallucination_total', 0)}")
    else:
        # Scan for all state keys
        chat_ids = []
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor=cursor, match="state:*", count=100)
            for key in keys:
                try:
                    chat_ids.append(int(key.split(":")[1]))
                except (ValueError, IndexError):
                    continue
            if cursor == 0:
                break

        print(f"\nActive chats: {len(chat_ids)}")
        for cid in sorted(chat_ids)[:20]:  # Show first 20
            state = await store.load(cid)
            print(f"  chat={cid}: iter={state.iteration}, updated={state.last_updated or 'never'}")

    await r.aclose()


async def cmd_reset(args):
    """Reset agent state for a chat_id."""
    if not args.confirm:
        confirm = input(f"Reset ALL state for chat_id={args.chat_id}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    import redis.asyncio as aioredis
    from ai_native_crm.config import settings

    r = aioredis.from_url(settings.redis_url, decode_responses=True)

    keys = [
        f"state:{args.chat_id}",
        f"critical_facts:{args.chat_id}",
        f"metrics:{args.chat_id}",
        f"audit:{args.chat_id}",
    ]
    deleted = await r.delete(*keys)
    print(f"Deleted {deleted} keys for chat_id={args.chat_id}")

    await r.aclose()


def cmd_web(args):
    """Start the web dashboard."""
    import uvicorn
    print(f"Starting web dashboard on http://localhost:{args.port}")
    uvicorn.run("ai_native_crm.web.app:app", host="0.0.0.0", port=args.port, reload=True)


if __name__ == "__main__":
    main()
