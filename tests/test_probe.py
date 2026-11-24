from pathlib import Path

from tih.budget import BudgetGuard
from tih.ledger import Ledger
from tih.paths import RunPaths
from tih.probe import PROBE_STEPS, run_probe
from tih.providers.dry import DryRunTransport
from tih.providers.fal import GatewayContext
from tih.stages.registry import RunContext

ROOT = Path(__file__).parents[1]


def test_probe_calls_each_step_once(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    paths = RunPaths("lincoln", 1, root=tmp_path)
    ledger = Ledger(tmp_path / "lincoln" / "calls.jsonl")
    gateway = GatewayContext(paths, ledger, BudgetGuard(ledger), DryRunTransport(tmp_path / "dry"),
                             ROOT / "series" / "lincoln" / "models.yaml")
    results = run_probe(RunContext(paths=paths, gateway=gateway), "1.1")
    assert [r.step for r in results] == list(PROBE_STEPS)
    assert len(results) == 9 and all(r.status == "ok" for r in results), results
    assert len(ledger.current()) == 9
    report = (tmp_path / "lincoln" / "probe" / "report.md").read_text(encoding="utf-8")
    assert all(step in report for step in PROBE_STEPS)
