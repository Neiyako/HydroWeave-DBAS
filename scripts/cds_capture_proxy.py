#!/usr/bin/env python3
"""Small, safe mitmproxy recorder for CDS API metadata (no response bodies)."""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE = re.compile(r"(?:authorization|cookie|set-cookie|x-api-key|api[-_]?key|token|secret|password|credential|cdsapi[_-]?key)", re.I)
ALLOW_HEADERS = {"content-type", "content-length", "accept", "user-agent", "host", "date", "server", "retry-after", "location"}

def redact(value):
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if SENSITIVE.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", value)
    return value

def safe_url(url):
    p = urlsplit(str(url)); q = [(k, "<redacted>" if SENSITIVE.search(k) else v) for k,v in parse_qsl(p.query, keep_blank_values=True)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))

def body_info(raw, limit, redact_enabled=True):
    data = bytes(raw or b""); out = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if not data: return out
    if len(data) <= limit:
        try:
            value = json.loads(data.decode("utf-8"))
            out["json"] = redact(value) if redact_enabled else value
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = data[:limit].decode("utf-8", "replace")
            out["text_preview"] = re.sub(r"(?i)(authorization|token|api[_-]?key)\s*[:=]\s*[^\s]+", r"\1=<redacted>", text) if redact_enabled else text
    else: out["truncated"] = True
    return out

def request_fields(obj):
    found = {}
    def walk(x):
        if isinstance(x, dict):
            for k,v in x.items():
                lk = str(k).lower()
                if lk in {"dataset","variable","variables","year","month","day","time","area","pressure_level","product_type","data_format"}: found[lk] = v
                walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(obj); return found

addons = []

class CaptureProxy:
    def __init__(self, output=".", max_body_bytes=16384, redact_enabled=True):
        self.output=Path(output); self.max_body_bytes=max_body_bytes; self.redact_enabled=redact_enabled
        self.output.mkdir(parents=True, exist_ok=True); (self.output/"flows").mkdir(exist_ok=True)
        self.events=self.output/"events.jsonl"
    def _write(self, event):
        with self.events.open("a", encoding="utf-8") as f: f.write(json.dumps(event, ensure_ascii=False, sort_keys=True)+"\n")
    def _headers(self, headers):
        return {str(k).lower(): ("<redacted>" if self.redact_enabled and SENSITIVE.search(str(k)) else str(v)) for k,v in headers.items() if str(k).lower() in ALLOW_HEADERS or SENSITIVE.search(str(k))}
    def request(self, flow):
        req=flow.request; url=safe_url(req.pretty_url); raw=bytes(getattr(req,"raw_content",b"") or b"")
        info=body_info(raw,self.max_body_bytes,self.redact_enabled); fields=request_fields(info.get("json",{}))
        self._write({"event":"request","timestamp":time.time(),"method":req.method,"url":url,"host":urlsplit(url).netloc,"path":urlsplit(url).path,"headers":self._headers(req.headers),"body":info,"fields":fields,"capture_id":os.getenv("DBAS_CAPTURE_ID"),"request_hash":hashlib.sha256((req.method+" "+url+"\n"+info["sha256"]).encode()).hexdigest()})
    def response(self, flow):
        req=flow.request; res=flow.response; raw=bytes(getattr(res,"raw_content",b"") or b""); self._write({"event":"response","timestamp":time.time(),"method":req.method,"url":safe_url(req.pretty_url),"status":getattr(res,"status_code",None),"headers":self._headers(res.headers),"body":{"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"truncated":len(raw)>self.max_body_bytes},"error":None})
    def error(self, flow): self._write({"event":"error","timestamp":time.time(),"url":safe_url(flow.request.pretty_url),"error":str(flow.error) if flow.error else "unknown"})

# Use the executable entry point below so output settings are explicit.
addons = []

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--output",default="data/captures"); p.add_argument("--max-body-bytes",type=int,default=16384); p.add_argument("--redact",action=argparse.BooleanOptionalAction,default=True); p.add_argument("--self-test",action="store_true")
    a=p.parse_args(argv)
    if a.self_test: print(json.dumps({"url":safe_url("https://x.test/?token=secret&year=2020"),"body":body_info(b'{"api_key":"secret","year":"2020"}',a.max_body_bytes)})); return 0
    try:
        from mitmproxy import options
        from mitmproxy.tools.dump import DumpMaster
    except ImportError: p.error("运行代理需要 mitmproxy；离线测试可使用 --self-test")
    addon=CaptureProxy(a.output,a.max_body_bytes,a.redact); master=DumpMaster(options.Options(listen_host="127.0.0.1", listen_port=8080)); master.addons.add(addon)
    try: master.run()
    except KeyboardInterrupt: master.shutdown()
    return 0

if __name__ == "__main__": sys.exit(main())
