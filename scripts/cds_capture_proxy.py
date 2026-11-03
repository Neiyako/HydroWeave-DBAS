#!/usr/bin/env python3
"""Small, safe mitmproxy recorder for CDS API metadata (no response bodies).

只记录 CDS 主机的 HTTP 元数据：方法、路径、脱敏后的请求体、状态码、响应大小。
响应正文永远不落盘，凭证字段一律替换为 <redacted>。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE = re.compile(
    r"(?:authorization|cookie|set-cookie|x-api-key|api[-_]?key|token|secret|password|credential|cdsapi[_-]?key)",
    re.I)
ALLOW_HEADERS = {"content-type", "content-length", "accept", "user-agent",
                 "host", "date", "server", "retry-after", "location"}
DEFAULT_HOSTS = ["cds.climate.copernicus.eu"]


def redact(value):
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if SENSITIVE.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", value)
    return value


def safe_url(url):
    p = urlsplit(str(url))
    q = [(k, "<redacted>" if SENSITIVE.search(k) else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def body_info(raw, limit, redact_enabled=True):
    data = bytes(raw or b"")
    out = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if not data:
        return out
    if len(data) <= limit:
        try:
            value = json.loads(data.decode("utf-8"))
            out["json"] = redact(value) if redact_enabled else value
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = data[:limit].decode("utf-8", "replace")
            out["text_preview"] = re.sub(
                r"(?i)(authorization|token|api[_-]?key)\s*[:=]\s*[^\s]+",
                r"\1=<redacted>", text) if redact_enabled else text
    else:
        out["truncated"] = True
    return out


def request_fields(obj):
    """从任意嵌套结构里抽取可复用的 CDS 请求字段。"""
    found = {}
    keys = {"dataset", "variable", "variables", "year", "month", "day", "time",
            "area", "pressure_level", "product_type", "data_format"}

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if lk in keys:
                    found[lk] = v
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


addons = []


class CaptureProxy:
    def __init__(self, output=".", max_body_bytes=16384, redact_enabled=True, hosts=None):
        self.output = Path(output)
        self.max_body_bytes = max_body_bytes
        self.redact_enabled = redact_enabled
        self.hosts = [h.lower() for h in (hosts or DEFAULT_HOSTS)]
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "flows").mkdir(exist_ok=True)
        self.events = self.output / "events.jsonl"
        self.stats = {"request": 0, "response": 0, "error": 0, "ignored": 0}
        self._write_manifest()

    def _match(self, flow):
        host = (flow.request.host or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def _write_manifest(self):
        manifest = {
            "proxy": {"listen": "127.0.0.1", "hosts": self.hosts},
            "network_started": True,
            "max_body_bytes": self.max_body_bytes,
            "redact": self.redact_enabled,
            "started": time.time(),
            "credentials": "resolved by cdsapi at runtime; never recorded",
        }
        (self.output / "proxy_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write(self, event):
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self._flush_stats()

    def _flush_stats(self):
        """事件一落地就刷新统计，进程被 SIGKILL 也不会丢证据等级判定依据。"""
        path = self.output / "proxy_manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data["stats"] = dict(self.stats)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _headers(self, headers):
        return {
            str(k).lower(): ("<redacted>" if self.redact_enabled and SENSITIVE.search(str(k)) else str(v))
            for k, v in headers.items()
            if str(k).lower() in ALLOW_HEADERS or SENSITIVE.search(str(k))
        }

    def request(self, flow):
        if not self._match(flow):
            self.stats["ignored"] += 1
            return
        req = flow.request
        url = safe_url(req.pretty_url)
        raw = bytes(getattr(req, "raw_content", b"") or b"")
        info = body_info(raw, self.max_body_bytes, self.redact_enabled)
        fields = request_fields(info.get("json", {}) or {})
        self.stats["request"] += 1
        self._write({
            "event": "request", "timestamp": time.time(), "method": req.method,
            "url": url, "host": urlsplit(url).netloc, "path": urlsplit(url).path,
            "headers": self._headers(req.headers), "body": info, "fields": fields,
            "capture_id": os.getenv("DBAS_CAPTURE_ID"),
            "request_hash": hashlib.sha256(
                (req.method + " " + url + "\n" + info["sha256"]).encode()).hexdigest(),
        })

    def response(self, flow):
        if not self._match(flow):
            return
        req, res = flow.request, flow.response
        raw = bytes(getattr(res, "raw_content", b"") or b"")
        self.stats["response"] += 1
        self._write({
            "event": "response", "timestamp": time.time(), "method": req.method,
            "url": safe_url(req.pretty_url), "status": getattr(res, "status_code", None),
            "headers": self._headers(res.headers),
            "body": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                     "truncated": len(raw) > self.max_body_bytes},
            "error": None,
        })

    def error(self, flow):
        if not self._match(flow):
            return
        self.stats["error"] += 1
        self._write({
            "event": "error", "timestamp": time.time(),
            "url": safe_url(flow.request.pretty_url),
            "error": str(flow.error) if flow.error else "unknown",
        })

    def done(self):
        """退出时把统计写回 manifest，便于报告判定 proxy 层证据等级。"""
        path = self.output / "proxy_manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data.update({"stats": self.stats, "ended": time.time()})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


addons = []


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", default="data/captures")
    p.add_argument("--max-body-bytes", type=int, default=16384)
    p.add_argument("--redact", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--host", action="append", default=None,
                   help=f"只记录该 host，可重复；默认 {DEFAULT_HOSTS[0]}")
    p.add_argument("--port", type=int, default=8080, help="本地代理监听端口")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        print(json.dumps({
            "url": safe_url("https://x.test/?token=secret&year=2020"),
            "body": body_info(b'{"api_key":"secret","year":"2020"}', a.max_body_bytes),
        }, ensure_ascii=False))
        return 0
    try:
        from mitmproxy import options
        from mitmproxy.tools.dump import DumpMaster
    except ImportError:
        p.error("运行代理需要 mitmproxy；离线自检可用 --self-test")

    addon = CaptureProxy(a.output, a.max_body_bytes, a.redact, a.host)
    opts = options.Options(listen_host="127.0.0.1", listen_port=a.port)

    async def run():
        master = DumpMaster(opts, with_termlog=True, with_dumper=False)
        master.addons.add(addon)
        loop = asyncio.get_running_loop()
        # pkill/kill 发 SIGTERM 时 asyncio.run 不会走 finally，必须主动触发关闭，
        # 否则 proxy_manifest.json 里没有 stats，报告无法判断代理是否真的抓到流量。
        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(master.shutdown()))
        except (NotImplementedError, RuntimeError):
            signal.signal(signal.SIGTERM, lambda *_: addon.done())
        try:
            await master.run()
        finally:
            master.shutdown()
            addon.done()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
