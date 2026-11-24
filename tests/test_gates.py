import json

import pytest
from typer.testing import CliRunner

from tih.cli import app
from tih.gates import ArtifactChanged, GateNotApproved, approve, require_approved
from tih.paths import RunPaths

STORY_FILES = ("season_plan.yaml", "season_ledger.yaml", "bible.yaml")


@pytest.fixture
def paths(tmp_path):
    paths = RunPaths("demo", None, root=tmp_path)
    for name in STORY_FILES:
        (paths.stage_dir("story_room") / name).write_text(f"{name}: v1\n")
    return paths


def test_unapproved_gate_blocks(paths):
    with pytest.raises(GateNotApproved, match="tih approve season story"):
        require_approved(paths, "story")


def test_approved_gate_passes(paths):
    approve(paths, "story")
    require_approved(paths, "story")


def test_approval_records_a_hash_for_every_story_file(paths):
    record = approve(paths, "story")
    saved = json.loads(record.read_text())
    assert sorted(saved["artifacts"]) == sorted(f"story_room/{name}" for name in STORY_FILES)
    assert all(len(digest) == 64 for digest in saved["artifacts"].values())


def test_editing_season_ledger_requires_new_story_lock(paths):
    approve(paths, "story")
    (paths.stage_dir("story_room") / "season_ledger.yaml").write_text("changed\n")
    with pytest.raises(ArtifactChanged, match="season_ledger.yaml"):
        require_approved(paths, "story")
    approve(paths, "story")
    require_approved(paths, "story")


def test_approval_needs_every_file_to_exist(paths):
    (paths.stage_dir("story_room") / "bible.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="bible.yaml"):
        approve(paths, "story")


def test_unknown_gate_is_rejected(paths):
    with pytest.raises(KeyError, match="story"):
        approve(paths, "nonsense")


def test_cli_approves_the_story_gate(paths, tmp_path):
    result = CliRunner().invoke(
        app, ["approve", "season", "story", "--series", "demo", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "story: approved" in result.output
    require_approved(paths, "story")
