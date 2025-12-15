"""KomedoBot agent implementation.

This module provides an asynchronous function `run_agent` which sends a single
request to the OpenAI Responses API using the web_search tool. The agent reads
the prompt from ``prompt_system.txt`` and builds the appropriate request payload
to perform OCR (if an image is provided), search for the product composition on
the web, analyze the ingredients against a fixed list of comedogens, and вернуть
СТРОГО структурированный JSON (см. описание в prompt_system.txt).

The OpenAI API key is expected to be provided via the ``OPENAI_API_KEY``
environment variable. If it is not set, the OpenAI library will attempt to
fallback to other default authentication mechanisms.

Usage:

.. code-block:: python

    from agent.agent import run_agent
    raw_json = await run_agent(product_name="La Roche-Posay Effaclar Duo")
    # raw_json — строка с JSON, который потом парсится и форматируется ботом.
"""

import os
import base64
from typing import Optional, List, Dict, Any

from openai import OpenAI
import openai as openai_pkg
import logging

logger = logging.getLogger(__name__)
logger.info(f"Using openai package version: {getattr(openai_pkg, '__version__', 'unknown')}")


# Directory of this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load the system prompt from the text file located in this package
PROMPT_PATH = os.path.join(BASE_DIR, "prompt_system.txt")

try:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read()
except FileNotFoundError as exc:
    raise RuntimeError(
        f"System prompt file not found at {PROMPT_PATH!r}. Make sure the file exists."
    ) from exc

# Initialize the OpenAI client. The API key is read from the environment variable
# OPENAI_API_KEY. If not set, OpenAI library will try its default configuration.
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def _encode_image_to_base64(image_bytes: bytes) -> str:
    """Encode binary image data into a base64 string for embedding in the request."""
    return base64.b64encode(image_bytes).decode("utf-8")


async def run_agent(
    product_name: Optional[str] = None, image_bytes: Optional[bytes] = None
) -> str:
    """Analyze a cosmetic product and return a JSON string with structured result.

    This function constructs a single request to the OpenAI Responses API. The
    agent receives the user-provided product name and/or an image of the product,
    then, следуя prompt_system.txt:

    1. Пытается определить продукт (в т.ч. по фото, используя OCR).
    2. Ищет полный INCI состав.
    3. Анализирует ингредиенты по фиксированным спискам комедогенов.
    4. Формирует JSON-объект с полями product_name, risk_level, ingredients и т.д.

    Args:
        product_name: Optional product name supplied by the user.
        image_bytes: Optional raw bytes of the product photo.

    Returns:
        JSON string produced by the agent (один объект, без текста вокруг).
    """
    user_content: List[Dict[str, Any]] = []

    if product_name:
        user_content.append(
            {
                "type": "input_text",
                "text": f"Название продукта от пользователя: {product_name}",
            }
        )

    if image_bytes:
        b64 = _encode_image_to_base64(image_bytes)
        user_content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{b64}",
            }
        )

    if not user_content:
        user_content.append(
            {
                "type": "input_text",
                "text": "Данных о продукте нет, скажи, что не можешь выполнить анализ и верни JSON с error.",
            }
        )

    response = client.responses.create(
        model="gpt-5.2",
        instructions=SYSTEM_PROMPT,
        tools=[{"type": "web_search"}],
        input=[
            {
                "role": "user",
                "content": user_content,
            }
        ],
        max_output_tokens=2500,
        temperature=0,
    )

    # Ожидаем, что модель вернёт один JSON-объект как текст.
    return response.output_text
