"""fetch 模块测试（不联网，注入假 CDS 客户端）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dbas.fetch.cds import (FetchConfig, download_one,
                            enqueue_fetch_tasks, ensure_download_volume,
                            request_dedup_key)
from dbas_core.exceptions import FetchError

from tests.conftest import make_nc

FETCH_YAML = """
dataset: reanalysis-era5-single-levels-monthly-means
product_type: monthly_averaged_reanalysis
variables: [2m_temperature]
years: ["2024"]
months: ["01", "02"]
area: [40, 110, 30, 120]
target_dir: downloads
volume_id: downloads
"""


@pytest.fixture
def fcfg(tmp_path):
    p = tmp_path / "fetch.yaml"
    p.write_text(FETCH_YAML, encoding="utf-8")
    return FetchConfig.from_yaml(p)


def test_fetch_config_builds_requests(fcfg):
    reqs = fcfg.build_requests()
    assert len(reqs) == 2                       # 2 个月 = 2 个最小请求
    r = reqs[0]["request"]
    assert r["year"] == "2024" and r["month"] == "01"
    assert r["variable"] == ["2m_temperature"]
    assert r["area"] == [40, 110, 30, 120]


def test_fetch_config_requires_fields(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("dataset: x\n", encoding="utf-8")
    from dbas_core.exceptions import ConfigError
    with pytest.raises(ConfigError):
        FetchConfig.from_yaml(bad)


@pytest.mark.parametrize("area,msg", [
    ([30, 110, 40, 120], "北(30.0) ≤ 南"),      # 真实踩过的坑：盒子反了
    ([40, 110], "四个值"),
])
def test_fetch_config_rejects_bad_area(tmp_path, area, msg):
    """反向 area 会让 MARS 返回空数据，必须在入队前拦住。"""
    from dbas_core.exceptions import ConfigError
    p = tmp_path / "a.yaml"
    p.write_text(FETCH_YAML.replace("[40, 110, 30, 120]", str(area)),
                 encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        FetchConfig.from_yaml(p)
    assert msg in str(ei.value)


def test_enqueue_dedup(cat, fcfg, tmp_path):
    ensure_download_volume(cat, fcfg, tmp_path)
    created, skipped = enqueue_fetch_tasks(cat, fcfg)
    assert created == 2 and skipped == 0
    created2, skipped2 = enqueue_fetch_tasks(cat, fcfg)
    assert created2 == 0 and skipped2 == 2      # 重复执行不重复入队


class FakeCdsClient:
    """假客户端：写入一个合成 NetCDF 到目标路径。"""

    def __init__(self, content="nc"):
        self.content = content
        self.calls = []

    def retrieve(self, dataset, request, target):
        self.calls.append((dataset, request, target))
        make_nc(Path(target), n_time=1)


def test_download_registers_file_and_enqueues_metadata(cat, fcfg, tmp_path, monkeypatch):
    ensure_download_volume(cat, fcfg, tmp_path)
    enqueue_fetch_tasks(cat, fcfg)
    fake = FakeCdsClient()
    monkeypatch.setattr("dbas.fetch.cds.CLIENT_FACTORY", lambda: fake)
    tasks = cat.list_tasks(status="pending")
    params = __import__("json").loads(tasks[0]["params_json"])
    result = download_one(cat, params, tmp_path)
    assert Path(result["path"]).exists()
    # 文件已注册进 catalog
    f = cat.get_file(__import__("dbas_core.catalog.repository", fromlist=["make_file_id"])
                     .make_file_id("downloads", params["target_rel"]))
    assert f is not None and f["size_bytes"] == result["size_bytes"]
    # 已自动入队元数据解析任务（另有 1 个未执行的下载任务留在队列）
    assert cat.count_tasks_by_status().get("pending", 0) == 3
    meta_tasks = [t for t in cat.list_tasks(status="pending")
                  if t["type"] == "scan_metadata"]
    assert len(meta_tasks) == 1
    assert not list(Path(result["path"]).parent.glob("*.part"))


def test_download_atomic_no_part_left_on_failure(cat, fcfg, tmp_path, monkeypatch):
    """下载产物为空时：.part 被清理、不注册文件、抛 FetchError。"""
    ensure_download_volume(cat, fcfg, tmp_path)
    enqueue_fetch_tasks(cat, fcfg)

    class EmptyClient:
        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"")       # 空文件

    monkeypatch.setattr("dbas.fetch.cds.CLIENT_FACTORY", lambda: EmptyClient())
    tasks = cat.list_tasks(status="pending")
    params = __import__("json").loads(tasks[0]["params_json"])
    with pytest.raises(FetchError):
        download_one(cat, params, tmp_path)
    assert cat.count_files(volume_id="downloads") == 0
    target = tmp_path / "downloads" / params["target_rel"]
    assert not list(target.parent.glob("*.part"))


def test_dedup_key_stable(fcfg):
    r = fcfg.build_requests()[0]
    assert request_dedup_key(fcfg, r) == request_dedup_key(fcfg, r)
    r2 = dict(r)
    r2["request"] = {**r["request"], "month": "03"}
    assert request_dedup_key(fcfg, r) != request_dedup_key(fcfg, r2)


# ---- 归一化与错误分级（借鉴 cdsswarm 与 ecmwf/cdsapi 的做法） ----

def test_dedup_key_equivalent_forms_collapse(fcfg):
    """同一语义的不同写法必须折叠成一个 dedup_key，否则会重复下载。"""
    base = fcfg.build_requests()[0]
    # "2024" vs ["2024"]：CDS 视为等价，摘要也应相同
    r2 = dict(base)
    r2["request"] = {**base["request"], "year": ["2024"]}
    assert request_dedup_key(fcfg, base) == request_dedup_key(fcfg, r2)
    # 集合型参数顺序无关
    r3 = dict(base)
    r3["request"] = {**base["request"], "month": ["01"], "variable": ["a", "b"]}
    r4 = dict(base)
    r4["request"] = {**base["request"], "month": ["01"], "variable": ["b", "a"]}
    assert request_dedup_key(fcfg, r3) == request_dedup_key(fcfg, r4)


def test_dedup_key_preserves_area_order(fcfg):
    """area 是位置参数 [北, 西, 南, 东]，重排会变成另一个地理区域，不能归一化掉。"""
    base = fcfg.build_requests()[0]
    r2 = dict(base)
    r2["request"] = {**base["request"], "area": [40, 110, 30, 120]}
    r3 = dict(base)
    r3["request"] = {**base["request"], "area": [30, 110, 40, 120]}
    assert request_dedup_key(fcfg, r2) != request_dedup_key(fcfg, r3)


def test_normalize_request_shape():
    from dbas.fetch.cds import normalize_request
    out = normalize_request({"year": "2024", "day": ["02", "01"], "area": [40, 110, 30, 120]})
    assert out["year"] == ["2024"]              # 标量包成列表
    assert out["day"] == ["01", "02"]           # 集合型排序
    # 位置参数保序；值统一转字符串，保证 40 与 "40" 摘要一致
    assert out["area"] == ["40", "110", "30", "120"]


@pytest.mark.parametrize("msg,permanent", [
    ("RuntimeError: 403 Client Error: cost limits exceeded. Your request is too large", True),
    ("MarsNoDataError: no data available for this selection", True),
    ("Exception: You need to accept the terms (required_terms)", True),
    ("ConnectionError: Connection reset by peer", False),      # 网络抖动仍应重试
    ("HTTPError: 503 Service Unavailable", False),             # 服务端临时故障仍应重试
])
def test_is_permanent_fetch_error(msg, permanent):
    from dbas.fetch.cds import is_permanent_fetch_error
    assert is_permanent_fetch_error(msg) is permanent


def test_download_raises_permanent_on_cost_limit(cat, fcfg, tmp_path, monkeypatch):
    """403 成本超限：判定为永久失败，不浪费 6 轮重试。"""
    from dbas.fetch.cds import FetchPermanentError
    ensure_download_volume(cat, fcfg, tmp_path)
    enqueue_fetch_tasks(cat, fcfg)

    class CostLimitClient:
        def retrieve(self, dataset, request, target):
            raise RuntimeError("403 Client Error: cost limits exceeded, request is too large")

    monkeypatch.setattr("dbas.fetch.cds.CLIENT_FACTORY", lambda: CostLimitClient())
    params = __import__("json").loads(cat.list_tasks(status="pending")[0]["params_json"])
    with pytest.raises(FetchPermanentError) as ei:
        download_one(cat, params, tmp_path)
    assert ei.value.permanent is True
    assert isinstance(ei.value, FetchError)       # 向后兼容既有 except
