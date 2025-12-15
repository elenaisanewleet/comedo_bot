"""Configuration for KomedoBot."""

import os
from typing import Optional

from dotenv import load_dotenv

# Загружаем переменные из .env, если файл есть рядом с проектом
load_dotenv()

# Telegram bot token (required)
TELEGRAM_BOT_TOKEN: Optional[str] = os.environ.get("TELEGRAM_BOT_TOKEN")

# OpenAI API key
OPENAI_API_KEY: Optional[str] = os.environ.get("OPENAI_API_KEY")
