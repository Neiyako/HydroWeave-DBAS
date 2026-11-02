"""Application-layer CDS capture: structured, redacted JSONL events."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_SECRET = re.compile(r"authorization|cookie|set-cookie|api[-_]?key|token|secret|password|credential", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "<redacted>" if _SECRET.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", value)
    return value


def request_hash(dataset: str, request: dict[str, Any]) -> str:
    payload = json.dumps({"dataset": dataset, "request": request}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class CaptureConfig:
    directory: Path
    max_body_bytes: int = 16384
    capture_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_yaml(cls, path: Path) -> "CaptureConfig":
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(Path(raw.get("output", "data/captures")).expanduser(), int(raw.get("max_body_bytes", 16384)))


class JsonlWriter:
    def __init__(self, config: CaptureConfig):
        self.config = config
        self.config.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.config.directory / "events.jsonl"

    def write(self, event: dict[str, Any]) -> None:
        event = {"capture_id": self.config.capture_id, "timestamp": time.time(), **event}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(redact(event), ensure_ascii=False, sort_keys=True) + "\n")


class ObservedCdsClient:
    """Wrap a cdsapi-compatible client without changing retrieve semantics."""
    def __init__(self, client: Any, writer: JsonlWriter, task_id: str | None = None):
        self.client = client
        self.writer = writer
        self.task_id = task_id

    def retrieve(self, dataset: str, request: dict[str, Any], target: str) -> Any:
        rhash = request_hash(dataset, request)
        started = time.monotonic()
        self.writer.write({"event": "application_request", "stage": "retrieve", "task_id": self.task_id,
                           "dataset": dataset, "request": request, "request_hash": rhash,
                           "target": os.path.basename(target)})
        try:
            result = self.client.retrieve(dataset, request, target)
        except Exception as exc:
            self.writer.write({"event": "application_error", "stage": "retrieve", "task_id": self.task_id,
                               "dataset": dataset, "request_hash": rhash, "duration_ms": round((time.monotonic() - started) * 1000, 3),
                               "error_type": type(exc).__name__, "error": str(exc)})
            raise
        size = Path(target).stat().st_size if Path(target).exists() else 0
        self.writer.write({"event": "application_response", "stage": "retrieve", "task_id": self.task_id,
                           "dataset": dataset, "request_hash": rhash, "duration_ms": round((time.monotonic() - started) * 1000, 3),
                           "target_bytes": size, "result_type": type(result).__name__})
        return result


def observed_factory(factory: Callable[[], Any], config: CaptureConfig, task_id: str | None = None) -> Callable[[], ObservedCdsClient]:
    writer = JsonlWriter(config)
    return lambda: ObservedCdsClient(factory(), writer, task_id=task_id)
