"""
Unified process for Railway:
  1) FastAPI (web + admin + payment webhook)
  2) Original Telegram Bot (polling) — same DB volume

Webhook is served only by FastAPI to avoid port conflict.
The bot's start_webhook is skipped when RUN_MODE=unified.
"""
from __future__ import annotations

import os
import sys
import threading

# Ensure package path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_api():
    import uvicorn
    port = int(os.getenv("PORT") or "8080")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")


def run_bot():
    # Import original bot module and start main without its own webhook server
    bot_path = os.path.join(os.path.dirname(__file__), "bot", "bot_original.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("premium_bot", bot_path)
    bot = importlib.util.module_from_spec(spec)
    # Patch: disable internal flask webhook when unified
    os.environ.setdefault("SKIP_BOT_WEBHOOK", "1")
    spec.loader.exec_module(bot)

    # Monkey-patch start_webhook to no-op so only FastAPI serves /webhook
    bot.start_webhook = lambda: print("ℹ️ Bot webhook skipped (API handles payments)")
    bot.main()


if __name__ == "__main__":
    mode = (os.getenv("RUN_MODE") or "unified").strip().lower()
    if mode == "api":
        run_api()
    elif mode == "bot":
        run_bot()
    else:
        # Unified: API in background thread, bot in main (Telegram long-poll)
        t = threading.Thread(target=run_api, daemon=True)
        t.start()
        print("🌐 FastAPI started in background")
        run_bot()
