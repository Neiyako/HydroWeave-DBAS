"""fetch 模块测试（不联网，注入假 CDS 客户端）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from dbas.fetch.cds import (FetchConfig, CLIENT_FACTORY, download_one,
                            enqueue_fetch_tasks, ensure_download_volume,
                            request_dedup_key)
from dbas.core.exceptions import FetchError

from tests.conftest import make_nc

FETCH_YAML = """
dataset: reanalysis-era5-single-levels-monthly-means
product_type: monthly_averaged_reanalysis
variables: [2m_temperature]
years: ["2024"]
months: ["01", "02"]
area: [30, 110, 40, 120]
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
    assert r["area"] == [30, 110, 40, 120]


def test_fetch_config_requires_fields(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("dataset: x\n", encoding="utf-8")
    from dbas.core.exceptions import ConfigError
    with pytest.raises(ConfigError):
        FetchConfig.from_yaml(bad)


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
    f = cat.get_file(__import__("dbas.catalog.repository", fromlist=["make_file_id"])
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
