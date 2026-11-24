import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import fal_client
import httpx
from dotenv import dotenv_values

from tih.assets import ShotRef, _sha256, save_asset
from tih.budget import BudgetGuard
from tih.ledger import CallRecord, Ledger, fingerprint
from tih.paths import RunPaths
from tih.providers.pricing import read_cost
from tih.series import load_series

REFUSAL_TYPES = {"content_policy_violation", "content_policy", "safety_violation"}
REFUSAL_TEXT = re.compile(
    r"content.?policy|safety|sensitive|nsfw|moderation|prohibited|violat", re.I
)
POLL_SECONDS = 3.0
MAX_WAIT_SECONDS = 1800.0


class ProviderRefused(Exception):
    def __init__(self, reason: str, at_submit: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.at_submit = at_submit


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Transport(Protocol):
    def submit(self, endpoint: str, arguments: dict[str, Any]) -> str: ...

    def wait(self, endpoint: str, request_id: str) -> dict[str, Any]: ...

    def download(self, url: str, destination: Path) -> None: ...

    def upload(self, path: Path) -> str: ...


def is_refusal(status_code: int, error_type: str | None, message: str) -> bool:
    if not 400 <= status_code < 500:
        return False
    return error_type in REFUSAL_TYPES or bool(REFUSAL_TEXT.search(message))


def _translate(error: fal_client.FalClientHTTPError, at_submit: bool) -> Exception:
    message = str(error.message)
    if is_refusal(error.status_code, error.error_type, message):
        return ProviderRefused(message, at_submit=at_submit)
    return ProviderError(message, status_code=error.status_code)


def fal_key() -> str:
    key = os.environ.get("FAL_KEY") or dotenv_values(Path.cwd() / ".env").get("FAL_KEY")
    if not key:
        raise RuntimeError("FAL_KEY is not set")
    return key


class FalTransport:
    """Talks to fal. The key is read on the first real call, so a re-run whose
    takes are all reused from the ledger works without one."""

    def __init__(self) -> None:
        self._client: fal_client.SyncClient | None = None

    @property
    def client(self) -> fal_client.SyncClient:
        if self._client is None:
            self._client = fal_client.SyncClient(key=fal_key())
        return self._client

    def submit(self, endpoint: str, arguments: dict[str, Any]) -> str:
        try:
            return self.client.submit(endpoint, arguments).request_id
        except fal_client.FalClientHTTPError as error:
            raise _translate(error, at_submit=True) from error

    def wait(self, endpoint: str, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        try:
            while not isinstance(
                self.client.status(endpoint, request_id), fal_client.Completed
            ):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{endpoint} request {request_id} timed out")
                time.sleep(POLL_SECONDS)
            return self.client.result(endpoint, request_id)
        except fal_client.FalClientHTTPError as error:
            raise _translate(error, at_submit=False) from error

    def download(self, url: str, destination: Path) -> None:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)

    def upload(self, path: Path) -> str:
        return self.client.upload_file(path)


@dataclass
class GatewayContext:
    paths: RunPaths
    ledger: Ledger
    budget: BudgetGuard
    transport: Transport
    models_file: Path

    @classmethod
    def for_run(cls, paths: RunPaths) -> "GatewayContext":
        ledger = Ledger(paths.root / paths.series / "calls.jsonl")
        models_file = Path("series") / paths.series / "models.yaml"
        budget = load_series(paths.series).budget
        guard = BudgetGuard(ledger, warn=budget.warn, stop=budget.stop)
        return cls(paths, ledger, guard, FalTransport(), models_file)


def upload_local(ctx: GatewayContext, path: Path) -> str:
    """Upload a local file once and reuse its URL, so call fingerprints stay stable."""
    cache_file = ctx.paths.root / ctx.paths.series / "uploads.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    digest = _sha256(Path(path))
    if digest not in cache:
        cache[digest] = ctx.transport.upload(Path(path))
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    return cache[digest]


@dataclass
class GenerationResult:
    call_id: str
    status: str
    output_paths: list[Path] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    endpoint: str = ""
    over_warn: bool = False


def _is_input(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://", "data:"))


def _split_arguments(
    arguments: dict[str, Any],
) -> tuple[str, dict[str, Any], list[str], list[str]]:
    settings: dict[str, Any] = {}
    raw_inputs: list[str] = []
    for key, value in arguments.items():
        if key == "prompt":
            continue
        if _is_input(value):
            raw_inputs.append(value)
        elif isinstance(value, list) and value and all(_is_input(v) for v in value):
            raw_inputs.extend(value)
        else:
            settings[key] = value
    hashes = [hashlib.sha256(v.encode("utf-8")).hexdigest() for v in raw_inputs]
    logged = [
        f"data:sha256:{digest}" if value.startswith("data:") else value
        for value, digest in zip(raw_inputs, hashes)
    ]
    return str(arguments.get("prompt", "")), settings, logged, hashes


def _output_urls(payload: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "url" and isinstance(value, str) and value.startswith("https://"):
                urls.append(value)
            else:
                urls.extend(_output_urls(value))
    elif isinstance(payload, list):
        for item in payload:
            urls.extend(_output_urls(item))
    return urls


def _all_flagged(payload: dict[str, Any]) -> bool:
    flags = payload.get("has_nsfw_concepts")
    return isinstance(flags, list) and bool(flags) and all(flags)


def _payload_path(ctx: GatewayContext, call_id: str) -> Path:
    return ctx.paths.root / ctx.paths.series / "payloads" / f"{call_id}.json"


def _load_finished(ctx: GatewayContext, record: CallRecord) -> GenerationResult | None:
    if record.status != "ok":
        return None
    stored = _payload_path(ctx, record.call_id)
    outputs = [Path(p) for p in (record.output_file or "").split("; ") if p]
    if not stored.exists() or not all(path.exists() for path in outputs):
        return None
    payload = json.loads(stored.read_text(encoding="utf-8"))
    return GenerationResult(record.call_id, "ok", outputs, payload)


def _record_failure(
    ctx: GatewayContext, call_id: str, error: ProviderRefused | ProviderError
) -> GenerationResult:
    if isinstance(error, ProviderRefused):
        ctx.ledger.update(
            call_id, status="blocked", kept=False, reject_reason=error.reason
        )
        return GenerationResult(call_id, "blocked")
    fields: dict[str, Any] = {"status": "error", "kept": False, "reject_reason": str(error)}
    if error.status_code is not None and error.status_code >= 500:
        fields["cost_usd"] = 0.0
    ctx.ledger.update(call_id, **fields)
    return GenerationResult(call_id, "error")


def _attempt(
    ctx: GatewayContext,
    step: str,
    endpoint: str,
    arguments: dict[str, Any],
    shot_ref: ShotRef,
    estimate_usd: float,
    take: int,
    quantity: float | None,
    expects_files: bool,
) -> GenerationResult:
    prompt, settings, inputs, input_hashes = _split_arguments(arguments)
    settings = {**settings, "take": take}
    call_fingerprint = fingerprint(endpoint, settings, prompt, input_hashes)

    for earlier in ctx.ledger.current():
        if earlier.fingerprint == call_fingerprint and earlier.status in {
            "blocked",
            "empty",
        }:
            return GenerationResult(earlier.call_id, earlier.status)

    reusable = ctx.ledger.find_reusable(call_fingerprint)
    if reusable:
        finished = _load_finished(ctx, reusable)
        if finished:
            return finished
        call_id, request_id = reusable.call_id, reusable.request_id
    else:
        call_id = uuid.uuid4().hex
        reservation = ctx.budget.reserve(estimate_usd)
        if reservation.over_warn:
            print(
                f"Budget warning: spend passes ${ctx.budget.warn:.2f} "
                f"with this {step} call",
                file=sys.stderr,
            )
        try:
            ctx.ledger.append(
                CallRecord(
                    call_id=call_id,
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    episode=shot_ref.episode,
                    clip=shot_ref.clip,
                    shot=shot_ref.shot,
                    step=step,
                    provider="fal",
                    model=endpoint,
                    settings=settings,
                    prompt=prompt,
                    inputs=inputs,
                    status="submitted",
                    estimate_usd=estimate_usd,
                    fingerprint=call_fingerprint,
                    input_hashes=input_hashes,
                )
            )
        finally:
            reservation.release()
        try:
            request_id = ctx.transport.submit(endpoint, arguments)
        except (ProviderRefused, ProviderError) as error:
            return _record_failure(ctx, call_id, error)
        ctx.ledger.update(call_id, request_id=request_id)

    shot_ref = shot_ref if shot_ref.step else replace(shot_ref, step=step)
    started = time.monotonic()
    try:
        payload = ctx.transport.wait(endpoint, request_id)
    except (ProviderRefused, ProviderError) as error:
        return _record_failure(ctx, call_id, error)

    cost, cost_source = read_cost(
        ctx.models_file, endpoint, arguments, quantity, estimate_usd
    )
    elapsed = round(time.monotonic() - started, 3)
    if _all_flagged(payload):
        ctx.ledger.update(
            call_id,
            status="blocked",
            kept=False,
            reject_reason="provider safety filter flagged all outputs",
            cost_usd=cost,
            cost_source=cost_source,
            time_taken_s=elapsed,
        )
        return GenerationResult(call_id, "blocked", payload=payload)
    urls = _output_urls(payload)
    if expects_files and not urls:
        ctx.ledger.update(
            call_id,
            status="empty",
            kept=False,
            reject_reason="provider returned no output",
            cost_usd=cost,
            cost_source=cost_source,
            time_taken_s=elapsed,
        )
        return GenerationResult(call_id, "empty", payload=payload)

    outputs = [save_asset(url, ctx, shot_ref) for url in urls]
    stored = _payload_path(ctx, call_id)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ctx.ledger.update(
        call_id,
        status="ok",
        output_file="; ".join(str(path) for path in outputs) or None,
        output_hash="; ".join(_sha256(path) for path in outputs) or None,
        cost_usd=cost,
        cost_source=cost_source,
        time_taken_s=elapsed,
    )
    return GenerationResult(call_id, "ok", outputs, payload)


Fallback = str | tuple[str, dict[str, Any]]


def generate(
    ctx: GatewayContext,
    step: str,
    endpoint: str,
    arguments: dict[str, Any],
    shot_ref: ShotRef,
    estimate_usd: float,
    fallbacks: tuple[Fallback, ...] = (),
    take: int = 0,
    quantity: float | None = None,
    expects_files: bool = True,
) -> GenerationResult:
    """Make one logged, budget-checked provider call, using fallbacks on refusal.

    A fallback is an endpoint name (same arguments) or (endpoint, arguments),
    because different models often name the same setting differently.
    """
    result = GenerationResult("", "error")
    candidates = [(endpoint, arguments)]
    for fallback in fallbacks:
        candidates.append(fallback if isinstance(fallback, tuple) else (fallback, arguments))
    for candidate, candidate_arguments in candidates:
        result = _attempt(
            ctx,
            step,
            candidate,
            candidate_arguments,
            shot_ref,
            estimate_usd,
            take,
            quantity,
            expects_files,
        )
        result.endpoint = candidate
        if result.status not in {"blocked", "empty"}:
            return result
    return result
