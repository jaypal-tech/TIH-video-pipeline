"""Shared plumbing for the paid production stages.

Every paid stage follows the same pattern:
1. check the owner's approvals (Script Lock, Look Lock),
2. build arguments from pipeline.yaml and the locked bible,
3. call generate(), which reserves budget and logs to the ledger,
4. write <stage>/takes.yaml listing every take, which one was chosen and why
   the others were not, and mark kept/rejected in the ledger.

Re-running a stage with --force costs nothing for takes that already exist:
generate() finds the earlier call by its fingerprint and reuses the output.
"""

import re
import string
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tih.assets import ShotRef
from tih.bible import load_bible
from tih.ledger import Ledger
from tih.providers.fal import GatewayContext, GenerationResult, generate
from tih.series import load_series
from tih.shots import PlanError, ResolvedPlan, load_plan, plan_path, resolve_plan
from tih.stages.registry import RunContext
from tih.story.models import Episode


class FallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str
    arguments: dict[str, Any]


class StepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str
    estimate_usd: float
    takes: int = 1
    arguments: dict[str, Any]
    fallbacks: list[FallbackConfig] = Field(default_factory=list)
    durations: list[int] = Field(default_factory=list)
    pause_token: str | None = None


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: dict[str, StepConfig]


class Take(BaseModel):
    take: int
    call_id: str
    status: str
    endpoint: str
    path: str | None = None
    reason: str | None = None  # why it was rejected; None means usable
    note: str | None = None  # anything the owner needs to choose (seed, voice id)


class TakeSet(BaseModel):
    chosen: str | None = None
    chosen_call_id: str | None = None
    takes: list[Take] = Field(default_factory=list)


class NoUsableTake(RuntimeError):
    """Every take for a shot failed or was rejected."""


WHOLE = re.compile(r"^\{([a-z_]+)\}$")


def load_pipeline(series: str) -> PipelineConfig:
    path = Path("series") / series / "pipeline.yaml"
    return PipelineConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def render_arguments(template: Any, values: dict[str, Any]) -> Any:
    """Fill an argument template. Keys whose value is None are dropped."""
    if isinstance(template, dict):
        rendered = {key: render_arguments(value, values) for key, value in template.items()}
        return {key: value for key, value in rendered.items() if value is not None}
    if isinstance(template, list):
        return [render_arguments(item, values) for item in template]
    if isinstance(template, str):
        whole = WHOLE.match(template)
        if whole:
            return values.get(whole.group(1))
        names = [name for _, name, _, _ in string.Formatter().parse(template) if name]
        if any(values.get(name) is None for name in names):
            missing = [name for name in names if values.get(name) is None]
            raise KeyError(f"Template needs {', '.join(missing)}")
        return template.format(**values)
    return template


def gateway_for(ctx: RunContext) -> GatewayContext:
    if ctx.gateway is not None:
        return ctx.gateway
    return GatewayContext.for_run(ctx.paths)


def load_resolved(ctx: RunContext) -> ResolvedPlan:
    """The resolved plan, re-checked against the current Director plan and script.

    Resolving is free, so every paid stage re-resolves; a plan or script that
    changed since `shotplan` ran stops the stage instead of paying for old shots.
    """
    scope = ctx.paths.scope_dir
    stored = ResolvedPlan.model_validate(
        yaml.safe_load((scope / "shotplan" / "shots.yaml").read_text(encoding="utf-8"))
    )
    script = Episode.model_validate(
        yaml.safe_load((scope / "writers_room" / "script.yaml").read_text(encoding="utf-8"))
    )
    current = resolve_plan(
        load_plan(plan_path(ctx.paths)), script, load_bible(ctx.paths.series),
        load_series(ctx.paths.series).runtime_s,
    )
    if current.model_dump(mode="json") != stored.model_dump(mode="json"):
        raise PlanError(
            "The shot plan or script changed since shotplan ran; re-run shotplan "
            f"(tih run {ctx.paths.episode} --stage shotplan --series {ctx.paths.series} --force)"
        )
    return current


def call_step(
    gateway: GatewayContext,
    config: StepConfig,
    step: str,
    values: dict[str, Any],
    shot_ref: ShotRef,
    take: int = 0,
    quantity: float | None = None,
    expects_files: bool = True,
) -> GenerationResult:
    arguments = render_arguments(config.arguments, values)
    fallbacks = tuple(
        (fallback.endpoint, render_arguments(fallback.arguments, values))
        for fallback in config.fallbacks
    )
    return generate(
        gateway,
        step,
        config.endpoint,
        arguments,
        shot_ref,
        config.estimate_usd,
        fallbacks=fallbacks,
        take=take,
        quantity=quantity,
        expects_files=expects_files,
    )


def take_from(
    result: GenerationResult, take: int, reason: str | None = None, note: str | None = None
) -> Take:
    path = str(result.output_paths[0]) if result.output_paths else None
    if result.status != "ok" and reason is None:
        reason = f"provider returned {result.status}"
    return Take(
        take=take,
        call_id=result.call_id,
        status=result.status,
        endpoint=result.endpoint,
        path=path,
        reason=reason,
        note=note,
    )


def picks_path(ctx: RunContext) -> Path:
    return ctx.paths.scope_dir / "picks.yaml"


def owner_pick(ctx: RunContext, stage: str, key: str) -> int | None:
    """The take number the owner chose with `tih pick`, if any."""
    path = picks_path(ctx)
    if not path.exists():
        return None
    picks = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return picks.get(stage, {}).get(key)


def save_pick(ctx: RunContext, stage: str, key: str, take: int) -> None:
    path = picks_path(ctx)
    picks = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    picks = picks or {}
    picks.setdefault(stage, {})[key] = take
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(picks, sort_keys=True), encoding="utf-8")


def choose(
    ctx: RunContext, ledger: Ledger, stage: str, key: str, takes: list[Take]
) -> TakeSet:
    """Pick the owner's take, else the first usable one; log kept/rejected."""
    usable = [t for t in takes if t.status == "ok" and t.path and not t.reason]
    if not usable:
        raise NoUsableTake(f"{stage} {key}: no usable take ({len(takes)} tried)")
    wanted = owner_pick(ctx, stage, key)
    chosen = next((t for t in usable if t.take == wanted), usable[0])
    for take in takes:
        if not take.call_id:
            continue
        if take is chosen:
            ledger.update(take.call_id, kept=True, reject_reason=None)
        elif take.status == "ok":
            ledger.update(take.call_id, kept=False, reject_reason=take.reason or "not chosen")
    return TakeSet(chosen=chosen.path, chosen_call_id=chosen.call_id, takes=takes)


def write_takes(stage_dir: Path, sets: dict[str, TakeSet]) -> None:
    data = {key: value.model_dump(mode="json") for key, value in sets.items()}
    (stage_dir / "takes.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )


def read_takes(ctx: RunContext, stage: str) -> dict[str, TakeSet]:
    path = ctx.paths.scope_dir / stage / "takes.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: TakeSet.model_validate(value) for key, value in data.items()}


def seed_for(base: int | None, take: int) -> int | None:
    return None if base is None else base + take
