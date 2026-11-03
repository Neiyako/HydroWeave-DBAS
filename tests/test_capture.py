from __future__ import annotations

import json
from pathlib import Path

from dbas.fetch.capture import CaptureConfig, JsonlWriter, ObservedCdsClient, redact, request_hash
from dbas.fetch.capture_report import summarize


def test_redact_and_hash_stable():
    value = redact({"Authorization": "Bearer secret", "year": "2024", "nested": {"api_key": "x"}})
    assert value["Authorization"] == "<redacted>"
    assert value["year"] == "2024"
    assert request_hash("d", {"year": "2024"}) == request_hash("d", {"year": "2024"})


def test_observed_client_writes_redacted_events(tmp_path):
    class Fake:
        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"nc")
            return {"ok": True}

    config = CaptureConfig(tmp_path)
    client = ObservedCdsClient(Fake(), JsonlWriter(config), task_id="task-1")
    target = tmp_path / "x.part"
    client.retrieve("dataset", {"variable": ["clwc"], "api_key": "secret"}, str(target))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    text = (tmp_path / "events.jsonl").read_text()
    assert "secret" not in text
    assert rows[0]["request_hash"]
    assert summarize(tmp_path)["requests"] == 1
    assert summarize(tmp_path)["responses"] == 1
    assert summarize(tmp_path)["cloud_water_coverage"]["clwc"]["captured"] is True


def test_split_by_day_keeps_scale_controllable():
    """按天拆分：CDS 单请求成本上限的规避手段，验证粒度和去重键。"""
    from dbas.fetch.cds import FetchConfig, request_dedup_key

    cfg = FetchConfig(
        dataset="reanalysis-era5-pressure-levels",
        variables=["specific_cloud_liquid_water_content"],
        years=["2010"], months=["05"],
        extra_request={"day": ["01", "02", "03"], "time": ["00:00", "12:00"],
                       "pressure_level": ["500"]},
        split_by="day")
    reqs = cfg.build_requests()
    assert len(reqs) == 3, reqs
    assert [r["request"]["day"] for r in reqs] == [["01"], ["02"], ["03"]]
    # 拆分后每个请求仍保留完整时次与层次，只是天维度被切开
    assert reqs[0]["request"]["time"] == ["00:00", "12:00"]
    assert reqs[0]["suffix"] == "d01"
    # 去重键必须互不相同，否则拆分会退化成只下一个文件
    keys = {request_dedup_key(cfg, r) for r in reqs}
    assert len(keys) == 3


def test_split_by_day_noop_when_single_day():
    from dbas.fetch.cds import FetchConfig

    cfg = FetchConfig(dataset="d", variables=["v"], years=["2024"], months=["01"],
                      extra_request={"day": ["01"], "time": ["00:00"]}, split_by="day")
    reqs = cfg.build_requests()
    assert len(reqs) == 1
    assert "suffix" not in reqs[0]


def test_proxy_redacts_token_and_keeps_fields():
    """脱敏脚本：凭证头必须替换，业务字段必须保留。"""
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "cds_capture_proxy.py"
    spec = importlib.util.spec_from_file_location("cds_capture_proxy", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cds_capture_proxy"] = mod
    spec.loader.exec_module(mod)

    url = mod.safe_url("https://cds.climate.copernicus.eu/x?token=abc&year=2024")
    assert "abc" not in url and "2024" in url

    info = mod.body_info(b'{"private-token":"abc","year":"2024"}', 1024)
    assert "abc" not in json.dumps(info)
    assert info["json"]["year"] == "2024"

    fields = mod.request_fields({"inputs": {"variable": ["clwc"], "day": ["01"]}})
    assert fields["variable"] == ["clwc"] and fields["day"] == ["01"]


def test_chunk_days_packs_by_cost_limit():
    """按成本上限分块：请求数必须显著下降，且每块不超 chunk_days。"""
    from dbas.fetch.cds import FetchConfig

    days = [f"{d:02d}" for d in range(1, 32)]
    cfg = FetchConfig(dataset="d", variables=["v"], years=["2010"], months=["05"],
                      extra_request={"day": days}, split_by="day", chunk_days=5)
    reqs = cfg.build_requests()
    assert len(reqs) == 7, len(reqs)          # ceil(31/5)
    assert reqs[0]["request"]["day"] == days[:5]
    assert reqs[0]["suffix"] == "d01-05"
    assert reqs[-1]["request"]["day"] == ["31"]
    assert reqs[-1]["suffix"] == "d31"        # 单天不回退成区间写法
    # 每个请求的天数都不超过 chunk_days
    assert all(len(r["request"]["day"]) <= 5 for r in reqs)


def test_chunk_days_default_is_one_day():
    from dbas.fetch.cds import FetchConfig

    cfg = FetchConfig(dataset="d", variables=["v"], years=["2010"], months=["05"],
                      extra_request={"day": ["01", "02"]}, split_by="day")
    assert cfg.chunk_days == 1
    assert len(cfg.build_requests()) == 2


# ---- 两段式抓包：submit / download 拆开观测 ----

class _FakeRemote:
    """模拟 cdsapi 的 Remote：带 request_id 与 content_length。"""

    def __init__(self, target_content=b"nc", request_id="req-123", content_length=2):
        self.request_id = request_id
        self.content_length = content_length
        self._content = target_content
        self.downloaded_to = None

    def download(self, target):
        Path(target).write_bytes(self._content)
        self.downloaded_to = target
        return target


class _FakeSubmitClient:
    """支持「只提交」语义的假客户端。"""

    def __init__(self, remote=None):
        self.remote = remote or _FakeRemote()
        self.submitted = []

    def retrieve(self, dataset, request, target=None):
        if target is None:
            self.submitted.append((dataset, request))
            return self.remote
        raise AssertionError("应走两段式，不该直接给 target")


def test_submit_and_download_are_separate_events(tmp_path):
    config = CaptureConfig(tmp_path)
    client = ObservedCdsClient(_FakeSubmitClient(), JsonlWriter(config), task_id="t-1")
    target = tmp_path / "x.part"
    client.retrieve("ds", {"variable": ["clwc"]}, str(target))

    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    stages = [(r["event"], r["stage"]) for r in rows]
    # 排队与传输分开记录，才能判断慢在队列还是链路
    assert ("application_request", "submit") in stages
    assert ("application_response", "submit") in stages
    assert ("application_response", "download") in stages

    submit_resp = next(r for r in rows
                       if r["event"] == "application_response" and r["stage"] == "submit")
    assert submit_resp["request_id"] == "req-123"
    assert submit_resp["content_length"] == 2

    dl = next(r for r in rows
              if r["event"] == "application_response" and r["stage"] == "download")
    assert dl["target_bytes"] == 2
    assert dl["size_matches"] is True
    assert dl["throughput_bps"] > 0
    # request_id 被记下来，供中断后复用服务端任务
    assert client.last_request_id == "req-123"


def test_truncated_download_flags_size_mismatch(tmp_path):
    """Actual < content_length 时要显式标出：netCDF 截断后往往仍能打开。"""
    config = CaptureConfig(tmp_path)
    remote = _FakeRemote(target_content=b"nc", content_length=999)
    client = ObservedCdsClient(_FakeSubmitClient(remote), JsonlWriter(config), task_id="t-2")
    client.retrieve("ds", {"variable": ["clwc"]}, str(tmp_path / "y.part"))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    dl = next(r for r in rows
              if r["event"] == "application_response" and r["stage"] == "download")
    assert dl["size_matches"] is False


def test_submit_error_records_error_event(tmp_path):
    """失败路径也要留证据，且异常必须原样抛出，不能被吞。"""
    class BoomClient:
        def retrieve(self, dataset, request, target=None):
            if target is None:
                raise RuntimeError("queue exploded")
            raise AssertionError("unreachable")

    config = CaptureConfig(tmp_path)
    client = ObservedCdsClient(BoomClient(), JsonlWriter(config), task_id="t-3")
    import pytest
    with pytest.raises(RuntimeError):
        client.retrieve("ds", {"variable": ["clwc"]}, str(tmp_path / "z.part"))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    err = next(r for r in rows if r["event"] == "application_error")
    assert err["stage"] == "submit"
    assert err["error_type"] == "RuntimeError"
    assert "duration_ms" in err


def test_legacy_client_falls_back_to_single_stage(tmp_path):
    """只接受三参数的老式客户端：退回单段路径，事件仍成对。"""
    class LegacyOnly:
        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"nc")
            return {"ok": True}

    config = CaptureConfig(tmp_path)
    client = ObservedCdsClient(LegacyOnly(), JsonlWriter(config), task_id="t-4")
    client.retrieve("ds", {"variable": ["clwc"]}, str(tmp_path / "w.part"))
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    events = [r["event"] for r in rows]
    assert events.count("application_request") == 1
    assert events.count("application_response") == 1
    assert client.last_request_id is None     # 老路径拿不到 request_id
