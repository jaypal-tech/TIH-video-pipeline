import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

SECRET_NAMES = ("FAL_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
JSON_COLUMNS = {"settings", "inputs", "input_hashes"}
REUSABLE_STATUSES = {"submitted", "ok"}


class CallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    episode: int | None = None
    clip: int | None = None
    shot: int | str | None = None
    step: str
    provider: str
    model: str
    settings: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    inputs: list[str] = Field(default_factory=list)
    output_file: str | None = None
    cost_usd: float | None = None
    time_taken_s: float | None = None
    kept: bool | None = None
    reject_reason: str | None = None
    call_id: str
    request_id: str | None = None
    status: Literal["submitted", "ok", "blocked", "empty", "error"]
    cost_source: Literal["unit_price", "usage", "estimated", "none"] = "none"
    estimate_usd: float | None = None
    fingerprint: str | None = None
    input_hashes: list[str] = Field(default_factory=list)
    output_hash: str | None = None


def fingerprint(
    endpoint: str, settings: dict[str, Any], prompt: str, input_hashes: list[str]
) -> str:
    payload = json.dumps(
        {
            "endpoint": endpoint,
            "settings": settings,
            "prompt": prompt,
            "input_hashes": input_hashes,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _secrets() -> list[str]:
    file_values = dotenv_values(Path.cwd() / ".env")
    values = {os.environ.get(name) for name in SECRET_NAMES}
    values |= {file_values.get(name) for name in SECRET_NAMES}
    return sorted((value for value in values if value), key=len, reverse=True)


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {_redact(k, secrets): _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


class Ledger:
    """Append-only JSONL record of every provider call."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: CallRecord) -> None:
        if any(existing.call_id == record.call_id for existing in self.current()):
            raise ValueError(f"Duplicate call_id: {record.call_id}")
        self._write({"event": "call", "record": record.model_dump()})

    def update(self, call_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(CallRecord.model_fields)
        if unknown:
            raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
        current = {record.call_id: record for record in self.current()}
        if call_id not in current:
            raise KeyError(f"Unknown call_id: {call_id}")
        CallRecord.model_validate({**current[call_id].model_dump(), **fields})
        self._write({"event": "update", "call_id": call_id, "fields": fields})

    def current(self) -> list[CallRecord]:
        records: dict[str, dict[str, Any]] = {}
        for event in self._events():
            if event["event"] == "call":
                records[event["record"]["call_id"]] = event["record"]
            else:
                records[event["call_id"]].update(event["fields"])
        return [CallRecord.model_validate(record) for record in records.values()]

    def find_reusable(self, fingerprint: str) -> CallRecord | None:
        for record in reversed(self.current()):
            if (
                record.fingerprint == fingerprint
                and record.request_id
                and record.status in REUSABLE_STATUSES
            ):
                return record
        return None

    def export_csv(self, path: Path) -> None:
        columns = list(CallRecord.model_fields)
        with Path(path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in self.current():
                row = record.model_dump()
                for column in JSON_COLUMNS:
                    row[column] = json.dumps(row[column], ensure_ascii=False)
                writer.writerow(row)

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        lines = data.split(b"\n")
        # Text after the last newline is a write interrupted by a crash.
        complete = lines[:-1]
        events = []
        for number, line in enumerate(complete, start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"Corrupt ledger at line {number}") from error
        return events

    def _write(self, event: dict[str, Any]) -> None:
        safe = _redact(event, _secrets())
        line = json.dumps(safe, ensure_ascii=False).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                data = handle.read()
                if data and not data.endswith(b"\n"):
                    handle.truncate(data.rfind(b"\n") + 1)
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
