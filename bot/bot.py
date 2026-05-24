"""
ComedoBot — Telegram bot (aiogram 3) — FINAL BALANCED VERSION

Логика:
- Шаг 1: краткий результат (риск + контекст) + 2 кнопки
- Кнопка 1: "Посмотреть состав" → полный список ингредиентов
- Кнопка 2: "Подробнее" → пояснение + рекомендации
"""

import asyncio
import json
import logging
import secrets
import time
import re
from typing import Any, Dict, List, Optional

import os
from aiohttp import web
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
# Render health server
# ─────────────────────────────────────────────────────────────

RENDER_PORT = int(os.getenv("PORT", "10000"))


async def _health_app() -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    return app


async def _run_health_server() -> None:
    app = await _health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=RENDER_PORT)
    await site.start()
    logging.info("Health server started on port %s", RENDER_PORT)


# ─────────────────────────────────────────────────────────────
# Визуальные разделители
# ─────────────────────────────────────────────────────────────

DIVIDER_LIGHT = "· · · · · · · · · · · · · · · · · · · · · · ·"
DIVIDER_ACCENT = "🫧 · · · · · · · · · · · · · · · · · · · 🫧"

# ─────────────────────────────────────────────────────────────
# Тексты (UX)
# ─────────────────────────────────────────────────────────────

START_MESSAGE = f"""Привет 🫧

Я помогаю оценить риск комедогенности — то есть вероятность забивания пор. 🤍

Пришли фото средства (упаковку или оборот с составом) или напиши название — бренд и продукт 🩵

В ответ ты получишь:
• уровень риска
• краткое пояснение
• кнопки: «Посмотреть состав» и «Подробнее» ✨

{DIVIDER_ACCENT}"""

HELP_MESSAGE = f"""<b>Как пользоваться</b> 🫧

Отправь фото средства или напиши его название.

Ты получишь:
• уровень риска комедогенности
• краткое пояснение
• кнопку «🧾 Посмотреть состав» — полный список ингредиентов с отметками
• кнопку «📘 Подробнее» — пояснение и рекомендации

{DIVIDER_LIGHT}

<b>Как интерпретировать результат</b> 🤍

🔴 <b>Высокий риск</b>
Есть компоненты, которые чаще провоцируют комедоны у склонной кожи.

🟠 <b>Средний риск</b>
Есть условно-комедогенные компоненты — возможна реакция при регулярном использовании.

🟡 <b>Низкий риск</b>
Есть отдельные условно-комедогенные компоненты — чаще переносится спокойно, но индивидуальная реакция возможна.

⚪️ <b>Риск не выявлен</b>
Комедогенные компоненты не обнаружены.

{DIVIDER_LIGHT}

<b>Важно</b> 🌸

Комедогенность — не абсолютная характеристика: реакция кожи индивидуальна и зависит от множества факторов.

Высокий риск не означает, что средство точно не подойдёт. Низкий риск не гарантирует отсутствие реакции.

Это инструмент для информированного выбора, а не диагностика.

{DIVIDER_LIGHT}

<b>Команды</b> 🫧

/about — о боте и ограничениях
/contacts — контакты по консультациям и работе бота

{DIVIDER_ACCENT}"""

ABOUT_MESSAGE = f"""<b>О боте</b> 🤍

{DIVIDER_LIGHT}

<b>Что это</b>

ComedoBot помогает ориентироваться в составе косметики и оценивать риск комедогенности.

{DIVIDER_LIGHT}

<b>Что это НЕ</b>

• Не медицинская консультация  
• Не диагностика состояния кожи  
• Не гарантия реакции или её отсутствия  
• Не повод отменять назначенное лечение

{DIVIDER_LIGHT}

<b>Куда за консультацией</b> 🩵

По вопросам кожи, консультаций и услуг: @DrDubinsky

{DIVIDER_LIGHT}

<b>По вопросам работы бота</b> 🤍

По вопросам работы бота, автоматизации и разработки сервисов: @elenaisanewleet

{DIVIDER_ACCENT}"""

CONTACTS_MESSAGE = f"""<b>Контакты</b> 🫧

{DIVIDER_LIGHT}

<b>Консультации по коже и услугам</b> 🩵

Если нужен разбор под твою кожу, консультация дерматолога или запись на услуги — напиши доктору Лизе Дубинской:

@DrDubinsky

{DIVIDER_LIGHT}

<b>Вопросы по работе бота и разработке</b> 🤍

Если бот работает некорректно, не нашёл состав, выдал странный результат или есть идея по улучшению — можно написать:

@elenaisanewleet

По вопросам работы бота, автоматизации и разработки сервисов: @elenaisanewleet

{DIVIDER_ACCENT}"""

PROCESSING_PHOTO = "🫧 Анализирую фото…"
PROCESSING_TEXT = "🫧 Ищу состав…"
PROCESSING_STEP2 = "🫧 Готовлю подробное пояснение…"
ERROR_GENERAL = "Не удалось обработать запрос. Попробуй ещё раз или отправь другое фото."
ERROR_EMPTY = "Отправь фото средства или напиши его название."


# ─────────────────────────────────────────────────────────────
# /base (открытая команда)
# ─────────────────────────────────────────────────────────────

def _build_base_message() -> str:
    lines: List[str] = []
    lines.append(f"{DIVIDER_ACCENT}\n")
    lines.append("<b>Справочник отмечаемых компонентов</b>\n")
    lines.append(f"{DIVIDER_LIGHT}\n")

    lines.append("🔴 <b>Жёсткие комедогенные компоненты</b>")
    lines.append("Компоненты, которые чаще вызывают комедоны у склонной кожи.\n")
    for name in sorted(hard_comedogens):
        lines.append(f"• {name}")
    lines.append("")
    lines.append(DIVIDER_LIGHT)
    lines.append("")

    lines.append("🟠 <b>Условно-комедогенные компоненты</b>")
    lines.append("Их влияние чаще зависит от индивидуальной реакции кожи и способа использования.\n")
    for name in sorted(conditional_comedogens.keys()):
        lines.append(f"• {name}")

    lines.append(f"\n{DIVIDER_ACCENT}")
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
    "high": "🔴 <b>Высокий риск комедогенности</b>",
    "medium": "🟠 <b>Средний риск комедогенности</b>",
    "low": "🟡 <b>Низкий риск комедогенности</b>",
    "none": "⚪️ <b>Риск комедогенности не выявлен</b>",
}

RISK_SHORT = {
    "high": "🔴 высокий",
    "medium": "🟠 средний",
    "low": "🟡 низкий",
    "none": "⚪️ не выявлен",
}

RISK_CONTEXT = {
    "high": "В составе есть компоненты, которые чаще провоцируют комедоны у склонной кожи. Это не означает, что средство точно не подойдёт — реакция индивидуальна. Попробуй тест на небольшом участке и понаблюдай 3–7 дней. 🤍",
    "medium": "В составе есть условно-комедогенные компоненты. Если кожа склонна к комедонам, вводи средство постепенно и отслеживай реакцию. 🫧",
    "low": "Есть отдельные условно-комедогенные компоненты. Риск обычно невысокий, но индивидуальная реакция возможна. 🌸",
    "none": "Комедогенные компоненты не обнаружены. С точки зрения комедогенности состав выглядит спокойным. 🤍",
}

EARLY_CUTOFF = 5  # используется в строгой логике, но не объясняется пользователю

# ─────────────────────────────────────────────────────────────
# Валидация source_url (чтобы не показывать "битые" ссылки)
# ─────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"^https?://", flags=re.I)


def _normalize_source_url(value: Any) -> Optional[str]:
    """Возвращает URL только если это похоже на реальную ссылку."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    if not _URL_RE.match(url):
        return None
    # минимальная чистка от кавычек/пробелов
    url = url.strip(' "\'')
    if not _URL_RE.match(url):
        return None
    return url


def _norm_text_for_match(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


async def _validate_source_url(url: str, ingredients: List[Dict[str, Any]]) -> bool:
    """
    Быстрая проверка:
    - страница открывается (200)
    - на странице встречаются несколько ингредиентов из списка
    """
    if not url:
        return False

    # берем несколько первых "наиболее вероятных" ингредиентов (обычно они точно есть в INCI-блоке)
    sample_names: List[str] = []
    for ing in ingredients[:12]:
        name = (ing.get("name") or "").strip()
        if name and len(name) >= 4:
            sample_names.append(name)

    if len(sample_names) < 3:
        # если ингредиентов мало/непонятно, не блокируем ссылку излишне строго
        sample_names = [n for n in sample_names if n]

    try:
        timeout = httpx.Timeout(6.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return False
            text = _norm_text_for_match(resp.text)

        # считаем попадания по подстроке (не идеально, но достаточно, чтобы отсеять "не туда")
        hits = 0
        for n in sample_names:
            nn = _norm_text_for_match(n)
            if nn and nn in text:
                hits += 1
            if hits >= 3:
                return True

        # если совсем не нашли совпадений — вероятно это не страница состава
        return False

    except Exception:
        return False


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
STEP2_CACHE_TTL_SEC = 15 * 60
STEP2_INFLIGHT: Dict[str, float] = {}


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


def _build_step1_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Посмотреть состав", callback_data=f"composition:{token}")],
            [InlineKeyboardButton(text="📘 Подробнее", callback_data=f"step2:{token}")],
        ]
    )


def _mark_for_component(is_hard: bool, is_cond: bool) -> str:
    if is_hard:
        return "🔴"
    if is_cond:
        return "🟠"
    return "⚪️"


def _clean_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\bпо вашим данным\b[:,]?\s*", "", t, flags=re.IGNORECASE)
    return t


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _short_text(t: str, *, max_sentences: int = 2, max_chars: int = 500) -> str:
    t = _clean_text(t or "")
    if not t:
        return ""
    parts = _SENT_SPLIT.split(t)
    out = " ".join(parts[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip(" ,.;:—-") + "…"
    return out


def build_doctor_cta_footer() -> str:
    lines = [
        DIVIDER_LIGHT,
        "",
        "💭 <i>Информация для ориентира и не является медицинской консультацией.</i>",
        "💬 За консультацией по коже и услугам: @DrDubinsky",
        "",
        DIVIDER_ACCENT,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Формат сообщений
# ─────────────────────────────────────────────────────────────

def build_step1_brief_message(data: Dict[str, Any]) -> str:
    if data.get("error") == "no_inci":
        product_name = data.get("product_name") or "Продукт"
        lines = [
            DIVIDER_ACCENT,
            "",
            "<b>Состав не найден</b> 🤍",
            "",
            f"🧴 <b>{product_name}</b>",
            "",
            DIVIDER_LIGHT,
            "",
            "Не удалось найти достоверный состав в открытых источниках.",
            "",
            "<b>Что можно сделать:</b>",
            "",
            "• сделай фото оборотной стороны упаковки при хорошем освещении",
            "• проверь, чтобы текст состава был чётким и не размытым",
            "• отправь точное название (бренд + линейка + продукт)",
            "",
        ]
        lines.append(build_doctor_cta_footer())
        return "\n".join(lines)

    product_name = data.get("product_name") or "Продукт"
    risk_level = data.get("risk_level") or "none"

    lines = [
        DIVIDER_ACCENT,
        "",
        RISK_LABELS.get(risk_level, RISK_LABELS["none"]),
        "",
        f"🧴 <b>{product_name}</b>",
        "",
        DIVIDER_LIGHT,
        "",
        RISK_CONTEXT.get(risk_level, ""),
        "",
    ]
    lines.append(build_doctor_cta_footer())
    return "\n".join(lines)


def build_composition_message(data: Dict[str, Any]) -> str:
    product_name = data.get("product_name") or "Продукт"
    ingredients = data.get("ingredients") or []
    source_url = data.get("source_url")

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Состав</b> 🫧",
        "",
        f"🧴 <b>{product_name}</b>",
        "",
        DIVIDER_LIGHT,
        "",
    ]

    has_comedogens = any(bool(ing.get("is_hard") or ing.get("is_conditional")) for ing in ingredients)

    if has_comedogens:
        lines.append("<b>Отмеченные компоненты:</b>")
        lines.append("")
        for idx, ing in enumerate(ingredients, start=1):
            name = ing.get("name")
            if not name:
                continue
            is_hard = bool(ing.get("is_hard"))
            is_cond = bool(ing.get("is_conditional"))
            if is_hard or is_cond:
                mark = _mark_for_component(is_hard, is_cond)
                lines.append(f"{mark} {idx}. {name}")
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    lines.append("<b>Список ингредиентов:</b>")
    lines.append("")
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        mark = _mark_for_component(bool(ing.get("is_hard")), bool(ing.get("is_conditional")))
        lines.append(f"{mark} {idx}. {name}")

    lines.append("")
    lines.append(DIVIDER_LIGHT)
    lines.append("")

    lines.append("💭 <b>Обозначения:</b>")
    lines.append("")
    lines.append("🔴 — жёсткие комедогенные компоненты")
    lines.append("🟠 — условно-комедогенные компоненты")
    lines.append("⚪️ — не отмечены как комедогенные")

    # ВАЖНО: источник показываем только если это валидный URL
    if source_url:
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")
        lines.append("🔗 <b>Источник состава:</b>")
        lines.append(f'<a href="{source_url}">Открыть страницу</a>')

    lines.append("")
    lines.append(build_doctor_cta_footer())

    return "\n".join(lines)


def build_step2_message(step2_data: Dict[str, Any], product_name: Optional[str] = None, risk_level: Optional[str] = None) -> str:
    summary = _short_text(step2_data.get("summary") or "", max_sentences=3, max_chars=650)
    overall = _short_text(step2_data.get("overall_notes") or "", max_sentences=2, max_chars=420)
    notes = step2_data.get("comedogens_notes") or []
    recs = step2_data.get("recommendations") or []

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Подробнее</b> 🩵",
        "",
    ]

    if product_name:
        lines.append(f"🧴 <b>{product_name}</b>")
    if risk_level:
        lines.append(f"🏷️ Уровень риска: <b>{RISK_SHORT.get(risk_level, '⚪️ не выявлен')}</b>")

    lines.append("")
    lines.append(DIVIDER_LIGHT)
    lines.append("")

    if summary:
        lines.append("<b>Коротко о результате</b> 🤍")
        lines.append("")
        lines.append(summary)
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    if notes:
        lines.append("<b>Что в составе может быть чувствительным для склонной кожи</b> 🫧")
        lines.append("")
        for item in notes[:5]:
            name = (item.get("name") or "").strip()
            typ = (item.get("type") or "").strip().lower()
            pos = item.get("position")
            note = _short_text(item.get("note") or "", max_sentences=1, max_chars=260)

            if not name:
                continue

            is_hard = (typ == "hard")
            is_cond = (typ == "conditional")
            mark = _mark_for_component(is_hard, is_cond)

            pos_txt = f" (№{pos})" if isinstance(pos, int) else ""
            lines.append(f"{mark} <b>{name}{pos_txt}</b>")
            if note:
                lines.append(note)
            lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    if overall and not summary:
        lines.append("<b>Общая оценка</b> 🌸")
        lines.append("")
        lines.append(overall)
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    if recs:
        lines.append("<b>Рекомендации</b> ✨")
        lines.append("")
        for r in recs[:5]:
            rr = _short_text(str(r), max_sentences=2, max_chars=240)
            if rr:
                lines.append(f"• {rr}")
                lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    lines.append(build_doctor_cta_footer())

    return "\n".join(lines).strip() or "Не удалось сформировать пояснение."


# ─────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────

async def handle_start(msg: Message):
    await msg.answer(START_MESSAGE)


async def handle_help(msg: Message):
    await msg.answer(HELP_MESSAGE)


async def handle_about(msg: Message):
    await msg.answer(ABOUT_MESSAGE)


async def handle_contacts(msg: Message):
    await msg.answer(CONTACTS_MESSAGE)


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

        # ── Нормализуем и валидируем source_url (чтобы не показывать "битые" ссылки)
        if data.get("error") != "no_inci":
            su = _normalize_source_url(data.get("source_url"))
            if su and ingredients:
                ok = await _validate_source_url(su, ingredients)
                data["source_url"] = su if ok else None
            else:
                data["source_url"] = None

        answer = build_step1_brief_message(data)

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
            reply_markup = _build_step1_keyboard(token)

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


async def handle_composition_callback(cb: CallbackQuery):
    payload = cb.data or ""
    if not payload.startswith("composition:"):
        return

    token = payload.split(":", 1)[1]
    step1_data = _cache_get(token)
    if not step1_data:
        await cb.answer("Эта кнопка уже неактуальна. Отправь запрос заново.", show_alert=True)
        return

    await cb.answer()
    await cb.message.answer(build_composition_message(step1_data), reply_markup=_build_step1_keyboard(token))


async def _run_step2_background(bot: Bot, chat_id: int, step1_data: Dict[str, Any], token: str) -> None:
    try:
        raw2 = await run_agent_step2(step1_data)
        step2_json = _parse_agent_json(raw2)
        if not step2_json:
            await bot.send_message(chat_id, "Не удалось сформировать пояснение. Попробуй ещё раз.")
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
            await bot.send_message(chat_id, "Не удалось сформировать пояснение. Попробуй ещё раз.")
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
        await cb.answer("Эта кнопка уже неактуальна. Отправь запрос заново.", show_alert=True)
        return

    if token in STEP2_INFLIGHT:
        await cb.answer("Уже формирую ответ.", show_alert=False)
        return

    STEP2_INFLIGHT[token] = time.time()

    await cb.answer()
    await cb.message.answer(PROCESSING_STEP2)

    chat_id = cb.message.chat.id
    asyncio.create_task(_run_step2_background(bot, chat_id, step1_data, token))


# ─────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────

async def _main_async():
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
    dp.message.register(handle_contacts, Command("contacts"))
    dp.message.register(handle_base, Command("base"))

    dp.message.register(handle_photo, F.photo)
    dp.message.register(handle_text, F.text)

    dp.callback_query.register(handle_composition_callback, F.data.startswith("composition:"))
    dp.callback_query.register(handle_step2_callback, F.data.startswith("step2:"))

    logging.info("ComedoBot started (FINAL BALANCED UX)")

    await _run_health_server()
    await dp.start_polling(bot)


def main():
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
