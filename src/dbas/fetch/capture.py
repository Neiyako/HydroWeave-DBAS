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
from typing import Any
from collections.abc import Callable

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
    def from_yaml(cls, path: Path) -> CaptureConfig:
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


class _NeedLegacyPath(Exception):
    """内部信号：当前客户端不支持「只提交」语义，需退回单段 retrieve。

    刻意不对外暴露——它只在本模块的 submit/retrieve 之间传递，
    绝不能让调用方看到这个内部细节。
    """


class ObservedCdsClient:
    """Wrap a cdsapi-compatible client without changing retrieve semantics.

    分两段观测（借鉴 otel-contrib 的失败记录写法：异常先捕获、统一收尾、再原样抛出）：

    - `submit`   ：提交请求并排队，记录 `request_id` 与 `content_length`；
    - `download` ：实际传输，记录耗时算出真实速率。

    为什么要拆：`retrieve()` 把「排队」和「传输」的耗时混在一条记录里，
    看到 20 分钟无法判断是 CDS 队列拥堵还是链路慢。
    """

    def __init__(self, client: Any, writer: JsonlWriter, task_id: str | None = None):
        self.client = client
        self.writer = writer
        self.task_id = task_id
        self.last_request_id: str | None = None

    def submit(self, dataset: str, request: dict[str, Any]) -> Any:
        """只提交、不下载，返回可后续 `download()` 的对象。

        真实 cdsapi 允许 `retrieve(dataset, request)` 不带 target 并返回 Remote；
        但简化客户端（含测试替身）只接受三参数签名。这种情况抛 `_NeedLegacyPath`，
        由 `retrieve()` 退回单段老路径——行为与改造前完全一致，只是拿不到 request_id。
        """
        rhash = request_hash(dataset, request)
        started = time.monotonic()
        self.writer.write({"event": "application_request", "stage": "submit",
                           "task_id": self.task_id, "dataset": dataset,
                           "request": request, "request_hash": rhash})
        try:
            remote = self.client.retrieve(dataset, request)   # 不带 target = 只提交
        except TypeError as exc:
            # 不能把"缺 target"的 TypeError 和客户端内部真 TypeError 混为一谈，
            # 用调用签名说明判定：报错落在 retrieve 调用本身才算契约不匹配。
            if "target" in str(exc):
                raise _NeedLegacyPath from None
            self._write_error("submit", dataset, rhash, started, exc)
            raise
        except Exception as exc:
            self._write_error("submit", dataset, rhash, started, exc)
            raise
        rid = getattr(remote, "request_id", None)
        if rid:
            self.last_request_id = str(rid)
        self.writer.write({"event": "application_response", "stage": "submit",
                           "task_id": self.task_id, "dataset": dataset,
                           "request_hash": rhash, "request_id": self.last_request_id,
                           "duration_ms": round((time.monotonic() - started) * 1000, 3),
                           "content_length": self._content_length(remote),
                           "result_type": type(remote).__name__})
        return remote

    def download(self, remote: Any, dataset: str, request: dict[str, Any],
                 target: str) -> Any:
        """把已提交的 remote 下载到 target，记录实测速率。"""
        rhash = request_hash(dataset, request)
        started = time.monotonic()
        expected = self._content_length(remote)
        try:
            result = remote.download(target)
        except Exception as exc:
            self._write_error("download", dataset, rhash, started, exc, request_id=self.last_request_id)
            raise
        elapsed = max(time.monotonic() - started, 1e-9)
        size = Path(target).stat().st_size if Path(target).exists() else 0
        self.writer.write({"event": "application_response", "stage": "download",
                           "task_id": self.task_id, "dataset": dataset,
                           "request_hash": rhash, "request_id": self.last_request_id,
                           "target": os.path.basename(target),
                           "duration_ms": round(elapsed * 1000, 3),
                           "target_bytes": size, "content_length": expected,
                           # 速率便于判断是不是被限流；与 content_length 比对可发现截断
                           "throughput_bps": round(size / elapsed, 1),
                           "size_matches": (expected is None or size == expected),
                           "result_type": type(result).__name__})
        return result

    @staticmethod
    def _content_length(remote: Any) -> int | None:
        for attr in ("content_length",):
            value = getattr(remote, attr, None)
            if isinstance(value, int):
                return value
        results = getattr(remote, "results", None)
        value = getattr(results, "content_length", None)
        return value if isinstance(value, int) else None

    def _write_error(self, stage: str, dataset: str, rhash: str,
                     started: float, exc: Exception,
                     request_id: str | None = None) -> None:
        """失败路径也留证据：带耗时与错误类型，不吞异常。"""
        self.writer.write({"event": "application_error", "stage": stage,
                           "task_id": self.task_id, "dataset": dataset,
                           "request_hash": rhash, "request_id": request_id,
                           "duration_ms": round((time.monotonic() - started) * 1000, 3),
                           "error_type": type(exc).__name__, "error": str(exc)})

    def retrieve(self, dataset: str, request: dict[str, Any], target: str) -> Any:
        """兼容原有单段调用：内部拆成 submit + download，事件粒度更细。

        若底层客户端不支持「只提交」，退回它自己的单段 retrieve，
        并补记一条 response 事件，保证事件流仍是成对的 request/response。
        """
        try:
            remote = self.submit(dataset, request)
        except _NeedLegacyPath:
            return self._legacy_retrieve(dataset, request, target)
        return self.download(remote, dataset, request, target)

    def _legacy_retrieve(self, dataset: str, request: dict[str, Any],
                         target: str) -> Any:
        rhash = request_hash(dataset, request)
        started = time.monotonic()
        try:
            result = self.client.retrieve(dataset, request, target)
        except Exception as exc:
            self._write_error("retrieve", dataset, rhash, started, exc)
            raise
        size = Path(target).stat().st_size if Path(target).exists() else 0
        self.writer.write({"event": "application_response", "stage": "retrieve",
                           "task_id": self.task_id, "dataset": dataset,
                           "request_hash": rhash,
                           "target": os.path.basename(target),
                           "duration_ms": round((time.monotonic() - started) * 1000, 3),
                           "target_bytes": size,
                           "result_type": type(result).__name__})
        return result


def observed_factory(factory: Callable[[], Any], config: CaptureConfig, task_id: str | None = None) -> Callable[[], ObservedCdsClient]:
    writer = JsonlWriter(config)
    return lambda: ObservedCdsClient(factory(), writer, task_id=task_id)
