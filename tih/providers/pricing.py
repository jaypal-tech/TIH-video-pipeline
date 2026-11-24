import math
import re
from pathlib import Path
from typing import Any

import yaml

DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$")


def _duration_seconds(arguments: dict[str, Any]) -> float | None:
    value = arguments.get("duration")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = DURATION.match(str(value))
    return float(match.group(1)) if match else None


def token_cost(
    models_file: Path, model: str, input_tokens: int, output_tokens: int
) -> float:
    """Return the USD cost of a Claude call from its rates in models.yaml."""
    models = yaml.safe_load(Path(models_file).read_text(encoding="utf-8"))["endpoints"]
    entry = models[model]
    cost = input_tokens * entry["input_per_mtok"] + output_tokens * entry["output_per_mtok"]
    return round(cost / 1_000_000, 6)


def read_cost(
    models_file: Path,
    endpoint: str,
    arguments: dict[str, Any],
    quantity: float | None,
    estimate_usd: float,
) -> tuple[float, str]:
    """Return (cost, source) from models.yaml, or the estimate if it can't be priced."""
    models = {}
    if Path(models_file).exists():
        models = yaml.safe_load(Path(models_file).read_text(encoding="utf-8"))["endpoints"]
    entry = models.get(endpoint, {})
    unit = entry.get("unit")
    rate = entry.get("rate")
    if "rates" in entry:
        value = arguments.get(entry["tier_key"])
        rate = entry["rates"].get(str(value).lower() if isinstance(value, bool) else str(value))
    if rate is not None and arguments.get("elements"):
        rate *= entry.get("elements_multiplier", 1.0)

    seconds = _duration_seconds(arguments)
    text = arguments.get("text") or arguments.get("prompt")
    if quantity is not None:
        amount = quantity
    elif unit == "seconds":
        amount = seconds
    elif unit == "minutes":
        amount = None if seconds is None else seconds / 60
    elif unit == "1000 characters":
        amount = len(text) / 1000 if isinstance(text, str) else None
    elif unit == "cloned voices":
        amount = 1
    else:
        amount = None

    if amount is None or rate is None:
        return estimate_usd, "estimated"
    if unit == "seconds":
        amount += entry.get("padding_seconds", 0.0)
    if entry.get("round_up"):
        amount = math.ceil(amount)
    return round(amount * rate, 6), "unit_price"
