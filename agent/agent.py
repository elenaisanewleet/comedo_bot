# agent/agent.py
"""ComedoBot agent implementation (2 steps) + STRICT comedogen flags by code."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
import openai as openai_pkg
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .comedogen_base import hard_comedogens, conditional_comedogens

load_dotenv()

logger = logging.getLogger(__name__)
logger.info("Using openai package version: %s", getattr(openai_pkg, "__version__", "unknown"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROMPT_STEP1_PATH = os.path.join(BASE_DIR, "prompt_system.txt")
PROMPT_STEP2_PATH = os.path.join(BASE_DIR, "prompt_system_step2.txt")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Prompt file not found at {path!r}.") from exc


SYSTEM_PROMPT_STEP1 = _read_text(PROMPT_STEP1_PATH)
SYSTEM_PROMPT_STEP2 = _read_text(PROMPT_STEP2_PATH)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

# Важно: большой общий таймаут клиента (Step2 может быть долгим), без ретраев.
client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    max_retries=0,
    timeout=httpx.Timeout(300.0, connect=20.0),
)

# ─────────────────────────────────────────────
# STRICT matching helpers (by base only)
# ─────────────────────────────────────────────

_non_alnum = re.compile(r"[^a-z0-9\s-]+")
_spaces = re.compile(r"\s+")

# Производные кислот — НЕ считаются hard
_acid_derivative_pattern = re.compile(
    r"\b(palmitate|stearate|laurate|myristate|caprate|caprylate)\b",
    flags=re.I,
)


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = _non_alnum.sub(" ", text)
    text = _spaces.sub(" ", text).strip()
    return text


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _matches_phrase(term: str, ingredient_norm: str) -> bool:
    """term can be a single word or phrase."""
    t = _norm(term)
    if " " in t:
        return t in ingredient_norm
    return _has_word(ingredient_norm, t)


def classify_ingredient_strict(name: str, position: int) -> Dict[str, Any]:
    """
    STRICT classification (only by your fixed base + special rules from prompt):

    HARD:
      - any exact hard term match
      - wax special rule: if 'wax' is in ingredient NAME => hard
      - acids: ONLY exact "... acid" forms are hard; derivatives are NOT hard

    CONDITIONAL:
      - exact conditional term match
      - special partial matches allowed only for: sil / methicone / dimethicone
    """
    n = _norm(name)

    # ── HARD: wax special rule (in name)
    if "wax" in n and _has_word(n, "wax"):
        return {"is_hard": True, "is_conditional": False, "early_conditional": False}

    # ── HARD: strict list (with acid derivative exclusion)
    for term in hard_comedogens:
        t = _norm(term)

        # acids: only exact "... acid" entries are allowed as hard
        if t in (
            "palmitic acid",
            "stearic acid",
            "lauric acid",
            "myristic acid",
            "capric acid",
            "caprylic acid",
        ):
            if _matches_phrase(t, n):
                return {"is_hard": True, "is_conditional": False, "early_conditional": False}
            continue

        if _matches_phrase(t, n):
            # защита от ложных срабатываний на производные кислот (на всякий случай)
            if _acid_derivative_pattern.search(n) and ("acid" in t):
                continue
            return {"is_hard": True, "is_conditional": False, "early_conditional": False}

    # ── CONDITIONAL: strict list + partial rules
    for term, cutoff in conditional_comedogens.items():
        t = _norm(term)

        # "sil" — match as whole word only (so it doesn't catch "silica")
        if t == "sil":
            if _has_word(n, "sil"):
                return {"is_hard": False, "is_conditional": True, "early_conditional": position <= int(cutoff)}
            continue

        # "methicone"/"dimethicone" — partial match allowed
        if t in ("methicone", "dimethicone"):
            if t in n:
                return {"is_hard": False, "is_conditional": True, "early_conditional": position <= int(cutoff)}
            continue

        # others — exact/phrase match
        if _matches_phrase(t, n):
            return {"is_hard": False, "is_conditional": True, "early_conditional": position <= int(cutoff)}

    return {"is_hard": False, "is_conditional": False, "early_conditional": False}


def apply_comedogenic_flags_strict(ingredients: List[Dict[str, Any]]) -> None:
    """Mutates ingredients: sets is_hard/is_conditional strictly by the base."""
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name") or ""
        flags = classify_ingredient_strict(name, idx)
        ing["is_hard"] = bool(flags["is_hard"])
        ing["is_conditional"] = bool(flags["is_conditional"])
        ing["_early_conditional"] = bool(flags["early_conditional"])  # внутренний флаг для отладки


# ─────────────────────────────────────────────
# URL sanitation for source_url (Step 1 output)
# ─────────────────────────────────────────────

_URL_HTTP_RE = re.compile(r"^https?://", flags=re.I)
_URL_PROTOLESS_RE = re.compile(r"^(www\.)", flags=re.I)
_URL_SCHEMELESS_RE = re.compile(r"^//")


def _normalize_source_url(value: Any) -> Optional[str]:
    """
    Делает source_url безопасным:
    - принимает только реальные URL
    - нормализует //example.com -> https://example.com
    - нормализует www.example.com -> https://www.example.com
    - отсеивает "название продукта" и прочий текст
    """
    if not isinstance(value, str):
        return None

    url = value.strip()
    if not url:
        return None

    # если есть пробелы/переносы — почти наверняка это не URL (как в твоём кейсе)
    if any(ch in url for ch in (" ", "\n", "\t")):
        return None

    url = url.strip(' "\'()[]{}<>')
    if not url:
        return None

    if _URL_SCHEMELESS_RE.match(url):
        url = "https:" + url

    if _URL_PROTOLESS_RE.match(url):
        url = "https://" + url

    if not _URL_HTTP_RE.match(url):
        return None

    # минимальная защита от мусора типа "https://"
    if len(url) < 12:
        return None

    return url


# ─────────────────────────────────────────────
# Common helpers
# ─────────────────────────────────────────────

def _encode_image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _build_user_content(product_name: Optional[str], image_bytes: Optional[bytes]) -> List[Dict[str, Any]]:
    user_content: List[Dict[str, Any]] = []
    if product_name:
        user_content.append({"type": "input_text", "text": f"Название продукта от пользователя: {product_name}"})
    if image_bytes:
        b64 = _encode_image_to_base64(image_bytes)
        user_content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"})
    if not user_content:
        user_content.append({"type": "input_text", "text": "Данных о продукте нет. Верни JSON с error."})
    return user_content


def _safe_json_loads(s: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _strip_bullets(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[-•]\s*", "", line)
    line = re.sub(r"^\d+[.)]\s*", "", line)
    return line.strip()


def _parse_step2_marked_text_v2(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "summary": "",
        "comedogens_notes": [],
        "overall_notes": "",
        "recommendations": [],
    }
    if not text:
        return out

    t = text.strip()

    def _block(name: str) -> str:
        pattern = rf"{name}:\s*(.+?)(?:\n\s*\n(?:SUMMARY|COMEDOGENS|OVERALL|RECOMMENDATIONS):|\Z)"
        m = re.search(pattern, t, flags=re.S | re.I)
        return (m.group(1).strip() if m else "").strip()

    out["summary"] = _block("SUMMARY")
    out["overall_notes"] = _block("OVERALL")

    rec_block = _block("RECOMMENDATIONS")
    recs: List[str] = []
    if rec_block:
        for line in rec_block.splitlines():
            s = _strip_bullets(line)
            if s:
                recs.append(s)
    out["recommendations"] = recs[:10]

    com_block = _block("COMEDOGENS")
    notes: List[Dict[str, Any]] = []
    if com_block:
        for line in com_block.splitlines():
            s = _strip_bullets(line)
            if not s:
                continue

            parts = [p.strip() for p in s.split("|")]
            name = parts[0] if parts else ""
            pos: Optional[int] = None
            typ: Optional[str] = None
            note = ""

            for p in parts[1:]:
                pl = p.lower()
                if pl.startswith("pos="):
                    try:
                        pos = int(re.sub(r"[^0-9]", "", p))
                    except Exception:
                        pos = None
                elif pl.startswith("type="):
                    typ = p.split("=", 1)[1].strip().lower()
                elif pl.startswith("note="):
                    note = p.split("=", 1)[1].strip()

            if name:
                item: Dict[str, Any] = {"name": name}
                if pos is not None:
                    item["position"] = pos
                if typ in ("hard", "conditional"):
                    item["type"] = typ
                if note:
                    item["note"] = note
                notes.append(item)

    out["comedogens_notes"] = notes[:30]
    return out


# ─────────────────────────────────────────────
# Step 1
# ─────────────────────────────────────────────

async def run_agent_step1(product_name: Optional[str] = None, image_bytes: Optional[bytes] = None) -> str:
    user_content = _build_user_content(product_name, image_bytes)

    resp = await client.responses.create(
        model="gpt-5.2",
        instructions=SYSTEM_PROMPT_STEP1,
        tools=[{"type": "web_search"}],
        max_tool_calls=10,
        input=[{"role": "user", "content": user_content}],
        max_output_tokens=2500,
        temperature=0,
    )

    raw = (resp.output_text or "").strip()

    # Делаем флаги ДЕТЕРМИНИРОВАННЫМИ (строго по базе)
    obj = _safe_json_loads(raw)
    if not obj:
        return raw

    ingredients = obj.get("ingredients")
    if isinstance(ingredients, list) and ingredients:
        apply_comedogenic_flags_strict(ingredients)
        obj["ingredients"] = ingredients

    # ✅ ВАЖНО: source_url либо валидный URL, либо null
    obj["source_url"] = _normalize_source_url(obj.get("source_url"))

    # risk_level НЕ считаем тут — это делает bot.py (строго по правилам)
    return json.dumps(obj, ensure_ascii=False)


# ─────────────────────────────────────────────
# Step 2
# ─────────────────────────────────────────────

async def run_agent_step2(step1_payload: Dict[str, Any]) -> str:
    product_name = step1_payload.get("product_name")
    risk_level = step1_payload.get("risk_level")
    ingredients = step1_payload.get("ingredients") or []

    comedogens: List[Dict[str, Any]] = []
    inci_list: List[str] = []

    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        inci_list.append(name)
        if ing.get("is_hard") or ing.get("is_conditional"):
            comedogens.append(
                {
                    "name": name,
                    "position": idx,
                    "type": "hard" if ing.get("is_hard") else "conditional",
                }
            )

    payload = {
        "product_name": product_name,
        "risk_level": risk_level,
        "comedogens": comedogens,
        "inci": inci_list,
    }

    prompt_text_web = (
        "Данные шага 1 (источник истины). Не выдумывай ингредиенты.\n"
        "Сделай:\n"
        "1) SUMMARY — короткое пояснение результата.\n"
        "2) COMEDOGENS — по каждому комедогену: почему он может быть проблемным для пор (1–2 предложения).\n"
        "3) OVERALL — общий вывод по продукту.\n"
        "4) RECOMMENDATIONS — 3–7 практичных рекомендаций.\n\n"
        "Верни СТРОГО с маркерами:\n"
        "SUMMARY:\n"
        "<абзац>\n\n"
        "COMEDOGENS:\n"
        "- <Name> | pos=<N> | type=<hard|conditional> | note=<...>\n\n"
        "OVERALL:\n"
        "<абзац>\n\n"
        "RECOMMENDATIONS:\n"
        "- ...\n\n"
        "Данные:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    try:
        resp = await client.responses.create(
            model="gpt-5.2",
            instructions=SYSTEM_PROMPT_STEP2,
            tools=[{"type": "web_search"}],
            max_tool_calls=3,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt_text_web}]}],
            max_output_tokens=1200,
            temperature=0,
        )

        parsed = _parse_step2_marked_text_v2((resp.output_text or "").strip())
        if parsed.get("summary") and parsed.get("recommendations"):
            return json.dumps(parsed, ensure_ascii=False)

        logger.warning("STEP2 web_search returned bad format; fallback to JSON-mode without web_search")

    except Exception as e:
        logger.warning("STEP2 web_search failed; fallback without web_search: %s", e)

    prompt_text_json = (
        "json\n"
        "Данные шага 1 (источник истины). Не выдумывай ингредиенты.\n"
        "Верни СТРОГО JSON-объект:\n"
        "{"
        "\"summary\":\"...\","
        "\"comedogens_notes\":[{\"name\":\"...\",\"position\":1,\"type\":\"hard\",\"note\":\"...\"}],"
        "\"overall_notes\":\"...\","
        "\"recommendations\":[\"...\",\"...\"]"
        "}\n\n"
        "Требования:\n"
        "- summary: 1 абзац, спокойный тон, на 'ты'\n"
        "- comedogens_notes: по каждому комедогену из данных\n"
        "- overall_notes: 1 абзац про продукт в целом\n"
        "- recommendations: 3–7 пунктов, практично, без лечения\n\n"
        "Данные:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    resp2 = await client.responses.create(
        model="gpt-5.2",
        instructions=SYSTEM_PROMPT_STEP2,
        tools=[],
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt_text_json}]}],
        text={"format": {"type": "json_object"}},
        max_output_tokens=900,
        temperature=0,
    )
    return (resp2.output_text or "").strip()


async def run_agent(product_name: Optional[str] = None, image_bytes: Optional[bytes] = None) -> str:
    return await run_agent_step1(product_name=product_name, image_bytes=image_bytes)
