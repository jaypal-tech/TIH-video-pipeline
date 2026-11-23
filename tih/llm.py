import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from dotenv import dotenv_values
from pydantic import BaseModel, ValidationError

from tih.budget import BudgetGuard
from tih.ledger import CallRecord, Ledger, fingerprint
from tih.paths import RunPaths
from tih.providers.pricing import token_cost

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 32000
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LlmError(RuntimeError):
    """Claude did not return a finished, usable answer."""


def claude_key() -> str:
    values = dotenv_values(Path.cwd() / ".env")
    key = os.environ.get("CLAUDE_API_KEY") or values.get("CLAUDE_API_KEY")
    if not key:
        raise RuntimeError("CLAUDE_API_KEY is not set")
    return key


@dataclass
class LlmContext:
    paths: RunPaths
    ledger: Ledger
    budget: BudgetGuard
    client: Any
    models_file: Path

    @classmethod
    def for_run(cls, paths: RunPaths) -> "LlmContext":
        ledger = Ledger(paths.root / paths.series / "calls.jsonl")
        client = anthropic.Anthropic(api_key=claude_key())
        models_file = Path("series") / paths.series / "models.yaml"
        return cls(paths, ledger, BudgetGuard(ledger), client, models_file)


def _payload_path(ctx: LlmContext, call_id: str) -> Path:
    return ctx.paths.root / ctx.paths.series / "payloads" / f"{call_id}.json"


def ask_json(
    ctx: LlmContext,
    role: str,
    system: str,
    user: str,
    schema: type[SchemaT],
    take: int = 0,
) -> SchemaT:
    """Make one logged Claude call and return its validated structured answer."""
    settings = {
        "role": role,
        "schema": schema.__name__,
        "effort": EFFORT,
        "max_tokens": MAX_TOKENS,
        "take": take,
    }
    prompt = f"{system}\n\n{user}"
    call_fingerprint = fingerprint(MODEL, settings, prompt, [])
    earlier = ctx.ledger.find_reusable(call_fingerprint)
    if earlier and _payload_path(ctx, earlier.call_id).exists():
        stored = _payload_path(ctx, earlier.call_id).read_text(encoding="utf-8")
        return schema.model_validate_json(stored)

    estimate = token_cost(ctx.models_file, MODEL, len(prompt) // 3, MAX_TOKENS)
    call_id = uuid.uuid4().hex
    reservation = ctx.budget.reserve(estimate)
    try:
        ctx.ledger.append(
            CallRecord(
                call_id=call_id,
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                episode=ctx.paths.episode,
                step="script",
                provider="anthropic",
                model=MODEL,
                settings=settings,
                prompt=prompt,
                status="submitted",
                estimate_usd=estimate,
                fingerprint=call_fingerprint,
            )
        )
    finally:
        reservation.release()

    started = time.monotonic()
    try:
        with ctx.client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": EFFORT},
            output_format=schema,
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as error:
        ctx.ledger.update(
            call_id, status="error", kept=False, reject_reason=error.message, cost_usd=0.0
        )
        raise LlmError(f"Claude API error {error.status_code}: {error.message}") from error
    except (anthropic.APIError, ValidationError) as error:
        ctx.ledger.update(
            call_id, status="error", kept=False, reject_reason=type(error).__name__
        )
        raise LlmError(f"Claude call failed: {type(error).__name__}") from error

    cost = token_cost(
        ctx.models_file, MODEL, message.usage.input_tokens, message.usage.output_tokens
    )
    elapsed = round(time.monotonic() - started, 3)
    if message.stop_reason != "end_turn" or message.parsed_output is None:
        ctx.ledger.update(
            call_id,
            status="blocked" if message.stop_reason == "refusal" else "error",
            kept=False,
            reject_reason=f"stop_reason: {message.stop_reason}",
            cost_usd=cost,
            cost_source="usage",
            time_taken_s=elapsed,
            request_id=message.id,
        )
        raise LlmError(f"Claude stopped early: {message.stop_reason}")

    result: SchemaT = message.parsed_output
    stored = _payload_path(ctx, call_id)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False), "utf-8")
    ctx.ledger.update(
        call_id,
        status="ok",
        request_id=message.id,
        output_file=str(stored),
        cost_usd=cost,
        cost_source="usage",
        time_taken_s=elapsed,
    )
    return result
