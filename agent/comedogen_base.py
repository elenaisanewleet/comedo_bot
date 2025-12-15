# agent/comedogen_base.py
"""Strict comedogen base for ComedoBot (DO NOT CHANGE LISTS)."""

from __future__ import annotations

# Жёсткие (hard) — фиксированный список
hard_comedogens = {
    "lanolin",
    "petrolatum",
    "paraffinum",
    "kerosinum",
    "ceresin",
    "wax",
    "cera wax",
    "palmitic acid",
    "stearic acid",
    "lauric acid",
    "myristic acid",
    "capric acid",
    "caprylic acid",
    "olive oil",
    "soybean oil",
    "corn oil",
    "cottonseed oil",
    "sesame oil",
    "arachis oil",
}

# Условные (conditional) — фиксированный список
# "ранняя позиция" = ≤ 5 (у всех одинаково по твоей логике)
conditional_comedogens = {
    "shea butter": 5,
    "lanolin": 5,
    "squalene": 5,
    "squalane": 5,
    "grape seed oil": 5,
    "sil": 5,
    "methicone": 5,
    "dimethicone": 5,
}
