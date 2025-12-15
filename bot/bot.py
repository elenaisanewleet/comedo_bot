"""
ComedoBot — Telegram bot (aiogram 3)

Логика:
- Шаг 1: после фото/названия → показываем только результат:
  риск → название → отмеченные компоненты → состав (визуально ранжирован) → ссылка (если есть)
  (без пояснения и рекомендаций)
- Шаг 2: по кнопке → отдельным сообщением приходит пояснение и советы (в фоне, без таймаута).
"""

import asyncio
import json
import logging
import secrets
import time
import re
from typing import Any, Dict, List, Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)

from .config import TELEGRAM_BOT_TOKEN
from agent.agent import run_agent_step1, run_agent_step2
from agent.comedogen_base import hard_comedogens, conditional_comedogens

logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────────────────────
# Тексты (UX)
# ─────────────────────────────────────────────────────────────

START_MESSAGE = """Привет 👋

Пришли мне:
📸 фото бьюти-средства (можно лицевую и/или оборот)
или
✍️ название (бренд + продукт)

Я покажу уровень риска и подсвечу “подозрительные” компоненты в составе ✨
А подробный разбор и рекомендации — по кнопке 📘"""

HELP_MESSAGE = """Как пользоваться 👇

1) Отправь фото или название средства
2) В первом ответе будет:
   🟢🟡🔴 уровень риска
   ⚠️ что в составе может забивать поры (с позициями)
   🧾 весь состав с метками

Потом можно нажать:
📘 «Пояснение и рекомендации» — и придёт второй ответ (почему так + как лучше использовать)."""

ABOUT_MESSAGE = """О боте 🤖

ComedoBot помогает оценить риск “забивания пор” по составу косметики.

📌 Первый ответ — только результат.
📘 Пояснение и рекомендации — отдельной кнопкой.

Важно: это не медицинская консультация."""

# Обезличенно, без “ищу/сравниваю”
PROCESSING_PHOTO = "📸 Секунду… сейчас посмотрю ✨"
PROCESSING_TEXT = "🔎 Секунду… сейчас разберусь ✨"
PROCESSING_STEP2 = "📘 Готовлю пояснение и рекомендации…"
ERROR_GENERAL = "Упс, не получилось. Попробуй ещё раз 🙏"
ERROR_EMPTY = "Пришли фото или название средства 🙂"


# ─────────────────────────────────────────────────────────────
# /base (открытая команда)
# ─────────────────────────────────────────────────────────────

def _build_base_message() -> str:
    lines: List[str] = []
    lines.append("📚 <b>/base — список отмечаемых компонентов</b>\n")

    lines.append("🔴 <b>Жёсткие</b>")
    for name in sorted(hard_comedogens):
        lines.append(f"• {name}")
    lines.append("")

    lines.append("🟡 <b>Условные</b> (ранняя позиция ≤ 5)")
    for name, cutoff in sorted(conditional_comedogens.items()):
        lines.append(f"• {name} (≤ {cutoff})")

    return "\n".join(lines)


BASE_MESSAGE = _build_base_message()


# ─────────────────────────────────────────────────────────────
# Вспомогательное
# ─────────────────────────────────────────────────────────────

async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


RISK_LABELS = {
    "high": "🔴 <b>ВЫСОКИЙ РИСК</b>",
    "medium": "🟡 <b>СРЕДНИЙ РИСК</b>",
    "low": "🟢 <b>НИЗКИЙ РИСК</b>",
    "none": "⚪️ <b>РИСК НЕ ОБНАРУЖЕН</b>",
}

RISK_SHORT = {
    "high": "🔴 высокий",
    "medium": "🟡 средний",
    "low": "🟢 низкий",
    "none": "⚪️ не обнаружен",
}

EARLY_CUTOFF = 5  # ранняя позиция = ≤ 5


def calc_risk_level_strict(ingredients: List[Dict[str, Any]]) -> str:
    hard_positions: List[int] = []
    conditional_positions: List[int] = []
    early_conditionals: List[int] = []

    for idx, ing in enumerate(ingredients, start=1):
        if ing.get("is_hard"):
            hard_positions.append(idx)
        if ing.get("is_conditional"):
            conditional_positions.append(idx)
            if idx <= EARLY_CUTOFF:
                early_conditionals.append(idx)

    if hard_positions or len(early_conditionals) >= 2:
        return "high"
    if len(early_conditionals) == 1 and not hard_positions:
        return "medium"
    if conditional_positions and not early_conditionals and not hard_positions:
        return "low"
    if not hard_positions and not conditional_positions:
        return "none"
    return "none"


# Кэш для шага 2 (в памяти)
STEP2_CACHE: Dict[str, Dict[str, Any]] = {}
STEP2_CACHE_TTL_SEC = 15 * 60  # 15 минут
STEP2_INFLIGHT: Dict[str, float] = {}  # token -> ts (анти-дубль)


def _cache_put(step1_data: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(8)
    STEP2_CACHE[token] = {"ts": time.time(), "data": step1_data}
    return token


def _cache_get(token: str) -> Optional[Dict[str, Any]]:
    item = STEP2_CACHE.get(token)
    if not item:
        return None
    if time.time() - float(item.get("ts", 0)) > STEP2_CACHE_TTL_SEC:
        STEP2_CACHE.pop(token, None)
        STEP2_INFLIGHT.pop(token, None)
        return None
    return item.get("data")


def _cache_del(token: str) -> None:
    STEP2_CACHE.pop(token, None)
    STEP2_INFLIGHT.pop(token, None)


def _parse_agent_json(raw: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(raw)
    except Exception as e:
        logging.error("JSON parse error: %s", e)
        return None


def _build_step2_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 Пояснение и рекомендации", callback_data=f"step2:{token}")]
        ]
    )


def _mark_for_component(is_hard: bool, is_cond: bool, position: int) -> str:
    if is_hard:
        return "🔴"
    if is_cond:
        return "🟡⚡" if position <= EARLY_CUTOFF else "🟡"
    return "⚪"


def _clean_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t


# ─────────────────────────────────────────────────────────────
# Формат сообщений
# ─────────────────────────────────────────────────────────────

def build_step1_message(data: Dict[str, Any]) -> str:
    # если состав не удалось получить/прочитать
    if data.get("error") == "no_inci":
        product_name = data.get("product_name") or "Продукт"
        lines = [
            "😕 <b>Не получилось разобрать состав</b>",
            "",
            f"🧴 <b>{product_name}</b>",
            "",
            "Что можно сделать 👇",
            "• фото при хорошем свете",
            "• крупнее оборот (чтобы текст был читабельным)",
            "• или пришли точное название текстом ✍️",
        ]
        return "\n".join(lines)

    product_name = data.get("product_name") or "Продукт"
    risk_level = data.get("risk_level") or "none"
    ingredients = data.get("ingredients") or []
    source_url = data.get("source_url")

    lines: List[str] = []

    # 1) риск
    lines.append(RISK_LABELS.get(risk_level, RISK_LABELS["none"]))
    lines.append("")

    # 2) название
    lines.append(f"🧴 <b>{product_name}</b>")
    lines.append("")
    lines.append("")

    # 3) отмеченные компоненты
    lines.append("⚠️ <b>Что в составе может забивать поры</b>")
    found = False
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        is_hard = bool(ing.get("is_hard"))
        is_cond = bool(ing.get("is_conditional"))
        if is_hard or is_cond:
            found = True
            mark = _mark_for_component(is_hard, is_cond, idx)
            lines.append(f"{mark} {name} — {idx}")

    if not found:
        lines.append("✨ По составу — ничего явно “подозрительного” не вижу.")
    lines.append("")

    # 4) весь состав (с метками на каждой строке)
    lines.append("🧾 <b>Состав</b>")
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        mark = _mark_for_component(bool(ing.get("is_hard")), bool(ing.get("is_conditional")), idx)
        lines.append(f"{mark} {idx}. {name}")
    lines.append("")

    # 5) ссылка (без лишних оговорок)
    if source_url:
        lines.append("🔗 <b>Ссылка</b>")
        lines.append(f'<a href="{source_url}">{product_name}</a>')
        lines.append("")

    # 6) мини-легенда (чтобы было понятно с первого взгляда)
    lines.append("🧷 <i>Метки:</i> 🔴 высокий риск · 🟡⚡ условный (в начале состава) · 🟡 условный · ⚪ остальное")
    lines.append("")
    lines.append("👇 Хочешь понять «почему так» и как лучше использовать — жми 📘")

    return "\n".join(lines)


def build_step2_message(step2_data: Dict[str, Any], product_name: Optional[str] = None, risk_level: Optional[str] = None) -> str:
    summary = _clean_text(step2_data.get("summary") or "")
    overall = _clean_text(step2_data.get("overall_notes") or "")
    notes = step2_data.get("comedogens_notes") or []
    recs = step2_data.get("recommendations") or []

    lines: List[str] = []

    lines.append("📘 <b>Пояснение и рекомендации</b>")
    if product_name:
        lines.append(f"🧴 <b>{product_name}</b>")
    if risk_level:
        lines.append(f"🏷️ Риск: <b>{RISK_SHORT.get(risk_level, '⚪️ не обнаружен')}</b>")
    lines.append("")

    if summary:
        lines.append("🗣️ <b>Что это значит</b>")
        lines.append(summary)
        lines.append("")

    if notes:
        lines.append("🧪 <b>На что обратить внимание</b>")
        for item in notes[:12]:
            name = (item.get("name") or "").strip()
            pos = item.get("position")
            typ = (item.get("type") or "").strip().lower()
            note = _clean_text(item.get("note") or "")
            if not name:
                continue

            is_hard = (typ == "hard")
            is_cond = (typ == "conditional")
            pos_int = int(pos) if isinstance(pos, int) else None
            mark = _mark_for_component(is_hard, is_cond, pos_int or 999)

            # чуть более “воздушно”: название отдельно, пояснение отдельной строкой
            head = f"{mark} <b>{name}</b>"
            if pos_int:
                head += f" <i>(№{pos_int})</i>"
            lines.append(head)
            if note:
                lines.append(f"— {note}")
            lines.append("")  # пустая строка между пунктами

        # убираем лишний хвостовой перенос
        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")

    if overall:
        lines.append("✨ <b>В целом</b>")
        lines.append(overall)
        lines.append("")

    if recs:
        lines.append("✅ <b>Как использовать, чтобы было спокойнее</b>")
        for r in recs[:10]:
            rr = _clean_text(str(r))
            if rr:
                lines.append(f"☑️ {rr}")
        lines.append("")

    lines.append("🤍 Напомню: это не диагноз и не лечение — просто удобная подсветка по составу.")
    return "\n".join(lines).strip() or "😕 Не получилось сформировать пояснение."


# ─────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────

async def handle_start(msg: Message):
    await msg.answer(START_MESSAGE)


async def handle_help(msg: Message):
    await msg.answer(HELP_MESSAGE)


async def handle_about(msg: Message):
    await msg.answer(ABOUT_MESSAGE)


async def handle_base(msg: Message):
    await msg.answer(BASE_MESSAGE)


async def _run_step1_and_answer(msg: Message, bot: Bot, product_name: Optional[str], image_bytes: Optional[bytes]):
    status = await msg.answer(PROCESSING_PHOTO if image_bytes else PROCESSING_TEXT)
    try:
        raw = await run_agent_step1(product_name=product_name, image_bytes=image_bytes)
        data = _parse_agent_json(raw)
        if not data:
            await status.delete()
            return await msg.answer(ERROR_GENERAL)

        ingredients = data.get("ingredients") or []
        if ingredients and data.get("error") != "no_inci":
            data["risk_level"] = calc_risk_level_strict(ingredients)

        answer = build_step1_message(data)

        reply_markup: Optional[InlineKeyboardMarkup] = None
        if data.get("error") != "no_inci" and ingredients:
            token = _cache_put(
                {
                    "product_name": data.get("product_name"),
                    "risk_level": data.get("risk_level"),
                    "source_url": data.get("source_url"),
                    "ingredients": ingredients,
                }
            )
            reply_markup = _build_step2_keyboard(token)

        await status.delete()
        await msg.answer(answer, reply_markup=reply_markup)

    except Exception as e:
        logging.error("STEP1 ERROR: %s", e)
        try:
            await status.delete()
        except Exception:
            pass
        await msg.answer(ERROR_GENERAL)


async def handle_photo(msg: Message, bot: Bot):
    if not msg.photo:
        return await msg.answer(ERROR_EMPTY)

    photo = msg.photo[-1]
    try:
        image_bytes = await _download_photo(bot, photo)
    except Exception as e:
        logging.error("PHOTO DOWNLOAD ERROR: %s", e)
        return await msg.answer(ERROR_GENERAL)

    await _run_step1_and_answer(msg, bot, product_name=None, image_bytes=image_bytes)


async def handle_text(msg: Message, bot: Bot):
    text = (msg.text or "").strip()
    if not text:
        return await msg.answer(ERROR_EMPTY)

    if text.startswith("/"):
        return

    await _run_step1_and_answer(msg, bot, product_name=text, image_bytes=None)


async def _run_step2_background(bot: Bot, chat_id: int, step1_data: Dict[str, Any], token: str) -> None:
    try:
        raw2 = await run_agent_step2(step1_data)
        step2_json = _parse_agent_json(raw2)
        if not step2_json:
            await bot.send_message(chat_id, "😕 Не получилось сделать пояснение. Попробуй ещё раз.")
            return

        await bot.send_message(
            chat_id,
            build_step2_message(
                step2_json,
                product_name=step1_data.get("product_name"),
                risk_level=step1_data.get("risk_level"),
            ),
        )
    except Exception as e:
        logging.error("STEP2 BACKGROUND ERROR: %s", e)
        try:
            await bot.send_message(chat_id, "😕 Не получилось сделать пояснение. Попробуй ещё раз.")
        except Exception:
            pass
    finally:
        _cache_del(token)


async def handle_step2_callback(cb: CallbackQuery, bot: Bot):
    payload = cb.data or ""
    if not payload.startswith("step2:"):
        return

    token = payload.split(":", 1)[1]
    step1_data = _cache_get(token)
    if not step1_data:
        await cb.answer("Эта кнопка уже неактуальна 🙈", show_alert=True)
        return

    if token in STEP2_INFLIGHT:
        await cb.answer("Уже готовлю ✨", show_alert=False)
        return

    STEP2_INFLIGHT[token] = time.time()

    await cb.answer()
    await cb.message.answer(PROCESSING_STEP2)

    chat_id = cb.message.chat.id
    asyncio.create_task(_run_step2_background(bot, chat_id, step1_data, token))


# ─────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_about, Command("about"))
    dp.message.register(handle_base, Command("base"))

    dp.message.register(handle_photo, F.photo)
    dp.message.register(handle_text, F.text)

    dp.callback_query.register(handle_step2_callback, F.data.startswith("step2:"))

    logging.info("ComedoBot started")
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
