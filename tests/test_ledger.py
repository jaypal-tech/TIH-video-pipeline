import csv
import json

import pytest

from tih.ledger import CallRecord, Ledger, fingerprint


def rec(call_id, **fields):
    values = {
        "call_id": call_id,
        "timestamp": "2026-11-24T14:03:11Z",
        "episode": 2,
        "clip": 3,
        "shot": 7,
        "step": "video",
        "provider": "fal",
        "model": "kling-v3",
        "status": "submitted",
    }
    values.update(fields)
    return CallRecord(**values)


def test_ledger_is_append_only(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = Ledger(path)
    ledger.append(rec("c1", status="submitted"))
    original = path.read_bytes()
    ledger.update("c1", status="ok", cost_usd=0.94)
    assert path.read_bytes().startswith(original)
    assert len(path.read_bytes()) > len(original)
    assert ledger.current()[0].cost_usd == 0.94


def test_state_survives_reopen(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = Ledger(path)
    ledger.append(rec("c1"))
    ledger.update("c1", status="ok", kept=False, reject_reason="wrong face")
    current = Ledger(path).current()[0]
    assert (current.status, current.kept, current.reject_reason) == (
        "ok",
        False,
        "wrong face",
    )


def test_crash_after_submit_is_reusable(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = Ledger(path)
    ledger.append(rec("c1", status="submitted", request_id="fal-1", fingerprint="abc"))
    assert Ledger(path).find_reusable("abc").request_id == "fal-1"


def test_submitted_without_request_id_is_not_reusable(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(rec("c1", status="submitted", fingerprint="abc"))
    assert ledger.find_reusable("abc") is None


@pytest.mark.parametrize("status", ["blocked", "empty", "error"])
def test_failed_call_is_not_reusable(tmp_path, status):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(rec("c1", status=status, request_id="fal-1", fingerprint="abc"))
    assert ledger.find_reusable("abc") is None


def test_latest_reusable_call_wins(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(rec("c1", status="ok", request_id="old", fingerprint="abc"))
    ledger.append(rec("c2", status="ok", request_id="new", fingerprint="abc"))
    assert ledger.find_reusable("abc").request_id == "new"


def test_prompt_never_contains_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "secret-123")
    path = tmp_path / "c.jsonl"
    Ledger(path).append(rec("c1", prompt="x secret-123 y"))
    assert "secret-123" not in path.read_text()


def test_update_and_nested_settings_never_contain_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "secret-123")
    path = tmp_path / "c.jsonl"
    ledger = Ledger(path)
    ledger.append(rec("c1", settings={"headers": ["Key secret-123"]}))
    ledger.update("c1", reject_reason="failed with secret-123")
    assert "secret-123" not in path.read_text()


@pytest.mark.parametrize("name", ["FAL_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
def test_every_provider_key_is_redacted(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "secret-456")
    path = tmp_path / "c.jsonl"
    Ledger(path).append(rec("c1", prompt="x secret-456 y"))
    assert "secret-456" not in path.read_text()


def test_key_only_in_dotenv_file_is_redacted(tmp_path, monkeypatch):
    for name in ("FAL_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CLAUDE_API_KEY=secret-789 # my note\n")
    path = tmp_path / "c.jsonl"
    Ledger(path).append(rec("c1", prompt="x secret-789 y"))
    assert "secret-789" not in path.read_text()


def test_torn_final_line_is_ignored_and_next_write_is_clean(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = Ledger(path)
    ledger.append(rec("c1"))
    with path.open("a") as handle:
        handle.write('{"event": "update", "call_id"')
    assert [r.call_id for r in ledger.current()] == ["c1"]
    ledger.append(rec("c2"))
    assert [r.call_id for r in Ledger(path).current()] == ["c1", "c2"]


def test_corruption_before_final_line_raises(tmp_path):
    path = tmp_path / "calls.jsonl"
    ledger = Ledger(path)
    ledger.append(rec("c1"))
    text = path.read_text()
    path.write_text("not json\n" + text)
    with pytest.raises(ValueError, match="line 1"):
        ledger.current()


def test_update_unknown_call_raises(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    with pytest.raises(KeyError, match="missing"):
        ledger.update("missing", status="ok")


def test_update_unknown_field_raises(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(rec("c1"))
    with pytest.raises(ValueError, match="bogus"):
        ledger.update("c1", bogus=1)


def test_duplicate_call_id_raises(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(rec("c1"))
    with pytest.raises(ValueError, match="c1"):
        ledger.append(rec("c1"))


def test_export_csv_has_every_brief_field(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(
        rec(
            "c1",
            status="ok",
            settings={"duration": 5, "seed": 7},
            prompt="MCU Booth",
            inputs=["refs/booth.png"],
            output_file="ep02/clip03/take1.mp4",
            cost_usd=0.94,
            time_taken_s=182,
            kept=False,
            reject_reason="wrong face",
        )
    )
    out = tmp_path / "log.csv"
    ledger.export_csv(out)
    row = next(csv.DictReader(out.open()))
    for field in [
        "timestamp",
        "episode",
        "clip",
        "shot",
        "step",
        "provider",
        "model",
        "settings",
        "prompt",
        "inputs",
        "output_file",
        "cost_usd",
        "time_taken_s",
        "kept",
        "reject_reason",
    ]:
        assert field in row
    assert row["cost_usd"] == "0.94"
    assert json.loads(row["settings"]) == {"duration": 5, "seed": 7}
    assert json.loads(row["inputs"]) == ["refs/booth.png"]


def test_fingerprint_ignores_setting_order():
    first = fingerprint("fal/x", {"a": 1, "b": 2}, "prompt", ["h1"])
    second = fingerprint("fal/x", {"b": 2, "a": 1}, "prompt", ["h1"])
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "endpoint,settings,prompt,inputs",
    [
        ("fal/y", {"a": 1}, "prompt", ["h1"]),
        ("fal/x", {"a": 2}, "prompt", ["h1"]),
        ("fal/x", {"a": 1}, "other", ["h1"]),
        ("fal/x", {"a": 1}, "prompt", ["h2"]),
    ],
)
def test_fingerprint_changes_with_any_input(endpoint, settings, prompt, inputs):
    assert fingerprint(endpoint, settings, prompt, inputs) != fingerprint(
        "fal/x", {"a": 1}, "prompt", ["h1"]
    )
