"""Telegram bot for KomedoBot.

Минималистичный, аккуратный бот для оценки риска комедогенности косметики.
"""

import asyncio
import logging
import json
from typing import List

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, PhotoSize

from aiogram.client.default import DefaultBotProperties
from .config import TELEGRAM_BOT_TOKEN
from agent.agent import run_agent

import httpx

logging.basicConfig(level=logging.INFO)



# ═══════════════════════════════════════════════════════════════════
# ТЕКСТЫ ДЛЯ ПОЛЬЗОВАТЕЛЯ
# ═══════════════════════════════════════════════════════════════════

START_MESSAGE = """Привет 👋

Я помогу понять, насколько средство может забивать поры.

Пришли мне:
• Фото средства (тюбик, флакон, крышка — как удобно)
• Или название продукта

Я проанализирую состав и покажу уровень риска."""

HELP_MESSAGE = """Как пользоваться ботом

1. Пришли фото средства или напиши его точное название и бренд.
2. Я проанализирую состав на риск забивания пор.
3. В ответе ты увидишь уровень риска, потенциально проблемные компоненты и рекомендации.

Что означают риски:

🔴 Высокий — есть компоненты, которые часто связывают с забиванием пор  
🟡 Средний — есть условные комедогены в заметной концентрации  
🟢 Низкий — условные комедогены есть, но ближе к концу списка  
⚪️ Нет риска — комедогенные компоненты не обнаружены

Помни: каждая кожа индивидуальна, а бот не ставит диагнозы и не заменяет консультацию специалиста."""

ABOUT_MESSAGE = """О боте

Я помогаю оценивать риск забивания пор по составу косметики.

В ответе ты получаешь:
• Уровень риска
• Компоненты, которые дают этот риск
• Короткие рекомендации по использованию

Бот создан для информирования и не даёт медицинских назначений."""

BASE_MESSAGE = """База комедогенных компонентов

Жёсткие:
- lanolin
- petrolatum
- paraffinum
- kerosinum
- ceresin
- wax
- cera wax
- palmitic acid
- stearic acid
- lauric acid
- myristic acid
- capric acid
- caprylic acid
- olive oil
- soybean oil
- corn oil
- cottonseed oil
- sesame oil
- arachis oil

Условные:
- shea butter
- lanolin
- squalene
- squalane
- grape seed oil
- sil
- methicone
- dimethicone

─────────────
Команда для справки и отладки."""

PROCESSING_PHOTO = "Анализирую продукт по фото..."
PROCESSING_TEXT = "Анализирую продукт по названию..."
ERROR_GENERAL = "Не получилось проанализировать продукт. Попробуй ещё раз."
ERROR_EMPTY = "Пришли, пожалуйста, фото средства или его название."



# ═══════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНАЯ ЛОГИКА
# ═══════════════════════════════════════════════════════════════════

async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
    logging.info("PHOTO: начинаю скачивать фото…")
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
    logging.info(f"PHOTO: url файла: {url}")

    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        logging.info(f"PHOTO: фото скачано, размер = {len(resp.content)} байт")
        return resp.content


RISK_LABELS = {
    "high": "🔴 ВЫСОКИЙ РИСК",
    "medium": "🟡 СРЕДНИЙ РИСК",
    "low": "🟢 НИЗКИЙ РИСК",
    "none": "⚪️ РИСК ОТСУТСТВУЕТ",
}



def build_message_from_agent(raw: str) -> str:
    logging.info(f"AGENT RAW (первые 300 символов): {raw[:300]!r}")

    try:
        data = json.loads(raw)
    except Exception as e:
        logging.error(f"JSON parse error: {e}")
        return ERROR_GENERAL

    # ——————————————————————————————
    # СЛУЧАЙ: СОСТАВ НЕ НАЙДЕН
    # ——————————————————————————————
    if data.get("error") == "no_inci":
        product_name = data.get("product_name") or "Продукт"
        recs = data.get("recommendations") or [
            "Попробуй сделать другое фото средства.",
            "Или отправь точное название продукта текстом."
        ]

        lines = [
            "Состав не найден",
            "",
            f"<b>{product_name}</b>",
            "",
            "Не удалось получить состав этого средства.",
            "",
            "<b>Что можно сделать</b>"
        ]

        for r in recs:
            lines.append(f"• {r}")

        return "\n".join(lines)

    # ——————————————————————————————
    # СЛУЧАЙ: СОСТАВ НАЙДЕН
    # ——————————————————————————————
    product_name = data.get("product_name") or "Продукт"
    risk_level = data.get("risk_level") or "none"
    summary = (data.get("summary") or "").strip()
    recommendations = data.get("recommendations") or []
    ingredients = data.get("ingredients") or []
    source_url = data.get("source_url")

    lines = []

    lines.append(RISK_LABELS.get(risk_level, "⚪️ РИСК ОТСУТСТВУЕТ"))
    lines.append("")
    lines.append(f"<b>{product_name}</b>")
    lines.append("")

    if summary:
        lines.append(summary)
        lines.append("Компоненты с риском забивания пор.")
        lines.append("")

    # Комедогенныe компоненты
    lines.append("<b>Обнаружены комедогены</b>")

    com_lines = []
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        is_hard = ing.get("is_hard")
        is_cond = ing.get("is_conditional")
        if is_hard or is_cond:
            emoji = "🔴" if is_hard else "🟡"
            com_lines.append(f"{emoji} {name} — {idx}")

    if com_lines:
        lines.extend(com_lines)
    else:
        lines.append("Нет компонентов с риском забивания пор.")
    lines.append("")

    # Рекомендации
    if recommendations:
        lines.append("<b>Рекомендации</b>")
        for r in recommendations:
            lines.append(f"• {r}")
        lines.append("")

    # Состав продукта
    lines.append("🧴 <b>Состав продукта</b>")
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        is_hard = ing.get("is_hard")
        is_cond = ing.get("is_conditional")
        tail = " 🔴" if is_hard else (" 🟡" if is_cond else "")
        lines.append(f"{idx}. {name}{tail}")
    lines.append("")

    # Ссылка
    if source_url:
        lines.append("🔗 <b>Ссылка на продукт</b>")
        lines.append(f'<a href="{source_url}">{product_name}</a>')

    return "\n".join(lines)



# ═══════════════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════

async def handle_start(msg: Message):
    await msg.answer(START_MESSAGE)


async def handle_help(msg: Message):
    await msg.answer(HELP_MESSAGE)


async def handle_about(msg: Message):
    await msg.answer(ABOUT_MESSAGE)


async def handle_base(msg: Message):
    await msg.answer(BASE_MESSAGE)



async def handle_photo(msg: Message, bot: Bot):
    logging.info("PHOTO: получено фото от пользователя")
    photo = msg.photo[-1]

    status = await msg.answer(PROCESSING_PHOTO)

    try:
        image_bytes = await _download_photo(bot, photo)

        logging.info("PHOTO: вызываю run_agent()…")
        raw = await run_agent(product_name=None, image_bytes=image_bytes)
        logging.info("PHOTO: run_agent() вернул ответ")

        answer = build_message_from_agent(raw)
        logging.info("PHOTO: построен HTML-ответ")

        await status.delete()
        await msg.answer(answer)

    except Exception as e:
        logging.error(f"PHOTO ERROR: {e}")
        await status.delete()
        await msg.answer(ERROR_GENERAL)



async def handle_text(msg: Message):
    text = (msg.text or "").strip()
    logging.info(f"TEXT: получено название: {text!r}")

    if not text:
        return await msg.answer(ERROR_EMPTY)

    status = await msg.answer(PROCESSING_TEXT)

    try:
        logging.info("TEXT: вызываю run_agent()…")
        raw = await run_agent(product_name=text, image_bytes=None)
        logging.info("TEXT: run_agent() вернул ответ")

        answer = build_message_from_agent(raw)
        logging.info("TEXT: построен HTML-ответ")

        await status.delete()
        await msg.answer(answer)

    except Exception as e:
        logging.error(f"TEXT ERROR: {e}")
        await status.delete()
        await msg.answer(ERROR_GENERAL)



# ═══════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML")
    )

    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_about, Command("about"))
    dp.message.register(handle_base, Command("base"))

    dp.message.register(handle_photo, F.photo)
    dp.message.register(handle_text, F.text)

    logging.info("КомедоБот запущен")
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
