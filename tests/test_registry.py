import json

import pytest
from typer.testing import CliRunner

from tih import cli
from tih.paths import RunPaths
from tih.stages import registry
from tih.stages.registry import MissingStage, RunContext, Stage, register, run_stage


class CountingRun:
    def __init__(self, stage="a"):
        self.calls = 0
        self.stage = stage

    def __call__(self, ctx):
        self.calls += 1
        out = ctx.paths.stage_dir(self.stage) / "output.txt"
        out.write_text(f"run {self.calls}")


def noop(ctx):
    return None


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    monkeypatch.setattr(registry, "_STAGES", {})


@pytest.fixture
def tmp_ctx(tmp_path):
    return RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))


def test_episode_stage_dir_uses_padded_episode(tmp_path):
    path = RunPaths("lincoln", 1, root=tmp_path).stage_dir("record")
    assert path == tmp_path / "lincoln" / "ep01" / "record"
    assert path.is_dir()


def test_season_scope_uses_season_dir(tmp_path):
    path = RunPaths("lincoln", None, root=tmp_path).stage_dir("story_room")
    assert path == tmp_path / "lincoln" / "season" / "story_room"


def test_stage_refuses_when_requirement_missing(tmp_ctx):
    register(Stage("b", requires=("a",), gate=None, run=noop))
    with pytest.raises(MissingStage, match="a"):
        run_stage(tmp_ctx, "b")


def test_stage_runs_after_requirement_completes(tmp_ctx):
    register(Stage("a", requires=(), gate=None, run=CountingRun("a")))
    second = CountingRun("b")
    register(Stage("b", requires=("a",), gate=None, run=second))
    run_stage(tmp_ctx, "a")
    assert run_stage(tmp_ctx, "b") == "ran"
    assert second.calls == 1


def test_completed_stage_is_skipped(tmp_ctx):
    counting_run = CountingRun()
    register(Stage("a", requires=(), gate=None, run=counting_run))
    assert run_stage(tmp_ctx, "a") == "ran"
    assert run_stage(tmp_ctx, "a") == "skipped"
    assert counting_run.calls == 1


def test_force_reruns(tmp_ctx):
    counting_run = CountingRun()
    register(Stage("a", requires=(), gate=None, run=counting_run))
    run_stage(tmp_ctx, "a")
    assert run_stage(tmp_ctx, "a", force=True) == "ran"
    assert counting_run.calls == 2


def test_done_file_records_output_hashes(tmp_ctx):
    register(Stage("a", requires=(), gate=None, run=CountingRun()))
    run_stage(tmp_ctx, "a")
    done = json.loads((tmp_ctx.paths.stage_dir("a") / "DONE.json").read_text())
    assert list(done["outputs"]) == ["output.txt"]
    assert len(done["outputs"]["output.txt"]) == 64


def test_failed_stage_is_not_marked_done(tmp_ctx):
    def explode(ctx):
        raise RuntimeError("boom")

    register(Stage("a", requires=(), gate=None, run=explode))
    with pytest.raises(RuntimeError):
        run_stage(tmp_ctx, "a")
    assert not (tmp_ctx.paths.stage_dir("a") / "DONE.json").exists()


def test_forced_run_that_fails_clears_previous_done(tmp_ctx):
    state = {"fail": False}

    def sometimes(ctx):
        if state["fail"]:
            raise RuntimeError("boom")

    register(Stage("a", requires=(), gate=None, run=sometimes))
    run_stage(tmp_ctx, "a")
    state["fail"] = True
    with pytest.raises(RuntimeError):
        run_stage(tmp_ctx, "a", force=True)
    assert not (tmp_ctx.paths.stage_dir("a") / "DONE.json").exists()


def test_unknown_stage_names_available_stages(tmp_ctx):
    register(Stage("a", requires=(), gate=None, run=noop))
    with pytest.raises(KeyError, match="a"):
        run_stage(tmp_ctx, "missing")


def test_cli_runs_stage(tmp_path):
    counting_run = CountingRun()
    register(Stage("a", requires=(), gate=None, run=counting_run))
    result = CliRunner().invoke(
        cli.app,
        ["run", "1", "--stage", "a", "--series", "demo", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "ran" in result.output
    assert counting_run.calls == 1
