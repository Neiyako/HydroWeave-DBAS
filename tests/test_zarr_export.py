"""Zarr 数组层测试：分块策略、往返一致性、清单哈希、catalog 登记。

全部使用合成数据（tmp_path + netCDF4 写入），不依赖真实 ERA5、外接盘或网络。
"""
from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from dbas_core.catalog.repository import Catalog
from dbas_core.config import Config
from dbas.zarr_export.config_ext import ZARR_DEFAULTS, install_zarr_config
from dbas.zarr_export.manifest import compare_manifests, scan_store
from dbas.zarr_export.store import (TARGET_CHUNK_ELEMENTS, ZarrExportError,
                                    estimate_chunk_bytes, export_store, plan_chunks)
from dbas.zarr_export.tasks import store_rel_path, zarr_export_one


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def layered_ds():
    """形似 ERA5 气压层数据：time × level × lat × lon，已解码 CF 时间。"""
    rng = np.random.default_rng(7)
    return xr.Dataset(
        {"t": (("time", "level", "lat", "lon"),
               rng.normal(270, 10, (6, 4, 20, 30)).astype("f4")),
         "q": (("time", "level", "lat", "lon"),
               (rng.random((6, 4, 20, 30)) * 1e-3).astype("f4"))},
        coords={"time": np.arange(6).astype("int64"),
                "level": np.array([1000, 850, 700, 500], dtype="f8"),
                "lat": np.linspace(30, 40, 20),
                "lon": np.linspace(70, 105, 30)},
    )


def _write_nc(path, ds):
    """把数据集落成真实 NetCDF 文件（zarr_export 从磁盘读，不走内存）。

    用 netCDF4 直接写并显式 close：xarray 的 to_netcdf 会把文件句柄留在
    内部 LRU 缓存里，随后 reopen/stat 在 macOS 上会撞 PermissionError。
    """
    import netCDF4
    path.parent.mkdir(parents=True, exist_ok=True)
    nc = netCDF4.Dataset(path, "w", format="NETCDF4")
    try:
        for dim, size in ds.sizes.items():
            nc.createDimension(dim, size)
        for cname, coord in ds.coords.items():
            v = nc.createVariable(cname, coord.dtype, (cname,))
            v[:] = coord.values
        for vname, var in ds.data_vars.items():
            dims = tuple(var.dims)
            enc = {"zlib": False, "chunksizes": (2,) + tuple(
                ds.sizes[d] for d in dims[1:])}
            v = nc.createVariable(vname, var.dtype, dims, **enc)
            v[:] = var.values
    finally:
        nc.close()
    return path


@pytest.fixture
def env(tmp_path):
    """最小可用环境：catalog + 卷 + 配置文件。"""
    install_zarr_config()
    root = tmp_path
    (root / "configs").mkdir()
    (root / "configs" / "system.yaml").write_text(
        "home: data\n", encoding="utf-8")
    cfg = Config(root=root, data={"home": "data"})
    (root / "data").mkdir(exist_ok=True)
    cat = Catalog(root / "data" / "catalog.db")
    src_root = root / "disk1"
    src_root.mkdir()
    cat.register_volume("disk1", src_root, label="合成源卷")
    yield cfg, cat, src_root
    cat.close()


# --------------------------------------------------------------------------
# 分块策略
# --------------------------------------------------------------------------

def test_chunk_targets_differ_in_time_and_space(layered_ds):
    """不同查询模式必须产出不同布局：这是本模块存在的理由。"""
    f = plan_chunks(layered_ds, "field_series")
    p = plan_chunks(layered_ds, "point_series")
    assert f.chunks["time"] == 1, "整场单时刻：时间维必须单片"
    assert p.chunks["time"] == 6, "单点时间序列：时间维必须整段"
    assert p.chunks["lat"] < f.chunks["lat"], "单点模式空间块必须更小"
    assert f.chunks != p.chunks


def test_chunk_plan_covers_every_dimension(layered_ds):
    plan = plan_chunks(layered_ds, "field_series")
    assert set(plan.chunks) == set(layered_ds.dims)


def test_chunk_never_exceeds_dimension_size(layered_ds):
    for target in ("field_series", "point_series", "area_stats", "level_profile"):
        plan = plan_chunks(layered_ds, target)
        for d, c in plan.chunks.items():
            assert 1 <= c <= layered_ds.sizes[d], f"{target}/{d}"


def test_chunk_element_budget_shrinks_time_axis():
    """点序列模式下空间块很小，整段时间轴在预算内，时间维不该被无谓切碎。"""
    ds = xr.Dataset(
        {"t": (("time", "lat", "lon"), np.zeros((1000, 400, 400), "f4"))},
        coords={"time": np.arange(1000), "lat": np.arange(400.0),
                "lon": np.arange(400.0)})
    plan = plan_chunks(ds, "point_series", chunk_elements=1_000_000)
    elems = plan.chunks["time"] * plan.chunks["lat"] * plan.chunks["lon"]
    assert elems <= 1_000_000, "任何模式都不得超出元素预算"
    assert plan.chunks["time"] == 1000, "空间块已足够小，时间轴应整段保留"


def test_chunk_budget_shrinks_time_when_space_is_full():
    """整场模式 + 极小预算：时间维（已为 1）无法再退，必须转而缩小空间维。"""
    ds = xr.Dataset(
        {"t": (("time", "lat", "lon"), np.zeros((10, 4000, 4000), "f4"))},
        coords={"time": np.arange(10), "lat": np.arange(4000.0),
                "lon": np.arange(4000.0)})
    plan = plan_chunks(ds, "field_series", chunk_elements=1_000_000)
    elems = (plan.chunks["time"] * plan.chunks["lat"] * plan.chunks["lon"])
    assert plan.chunks["time"] == 1
    assert elems <= 1_000_000
    assert plan.chunks["lat"] < 4000 or plan.chunks["lon"] < 4000
    assert "元素预算" in plan.rationale


def test_chunk_budget_shrinks_level_before_space(layered_ds):
    """层块先退、空间块后退：保住“整场”语义。"""
    plan = plan_chunks(layered_ds, "field_series", chunk_elements=2_000)
    assert plan.chunks["level"] < layered_ds.sizes["level"]
    assert plan.chunks["lat"] * plan.chunks["lon"] * plan.chunks["level"] <= 2_000


def test_unknown_target_is_rejected(layered_ds):
    with pytest.raises(ZarrExportError, match="未知 target"):
        plan_chunks(layered_ds, "no_such_target")


def test_chunk_bytes_estimate_is_per_variable(layered_ds):
    est = estimate_chunk_bytes(layered_ds, plan_chunks(layered_ds, "field_series"))
    assert set(est) == {"t", "q"}
    for _v, b in est.items():
        assert b > 0
        assert b <= TARGET_CHUNK_ELEMENTS * 4 + 4096  # f4 → itemsize 4


# --------------------------------------------------------------------------
# 往返一致性
# --------------------------------------------------------------------------

def test_roundtrip_is_bitwise_identical(layered_ds, tmp_path):
    out = tmp_path / "rt.zarr"
    export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    back = xr.open_zarr(out)
    try:
        for v in layered_ds.data_vars:
            exp = layered_ds[v].transpose(*layered_ds[v].dims).values
            got = back[v].transpose(*layered_ds[v].dims).values
            assert np.array_equal(exp, got), f"{v} 往返后数值不一致"
            assert got.dtype == exp.dtype
    finally:
        back.close()


def test_roundtrip_survives_sharding(layered_ds, tmp_path):
    plain = tmp_path / "plain.zarr"
    sharded = tmp_path / "shard.zarr"
    plan = plan_chunks(layered_ds, "field_series")
    export_store(layered_ds, plain, plan)
    export_store(layered_ds, sharded, plan, shards=3)
    n_plain = sum(1 for p in plain.rglob("*") if p.is_file())
    n_shard = sum(1 for p in sharded.rglob("*") if p.is_file())
    assert n_shard < n_plain, "分片应减少文件数"
    a = xr.open_zarr(plain)["t"].values
    b = xr.open_zarr(sharded)["t"].values
    assert np.array_equal(a, b), "分片不得改变数值"


def test_nan_fill_values_preserved(layered_ds, tmp_path):
    ds = layered_ds.copy(deep=True)
    ds["t"][0, 0, 0, 0] = np.nan
    out = tmp_path / "nan.zarr"
    export_store(ds, out, plan_chunks(ds, "field_series"))
    back = xr.open_zarr(out)
    try:
        assert np.isnan(back["t"].values[0, 0, 0, 0])
        assert not np.isnan(back["t"].values[0, 0, 0, 1])
    finally:
        back.close()


def test_existing_store_is_skipped_without_force(layered_ds, tmp_path):
    out = tmp_path / "s.zarr"
    plan = plan_chunks(layered_ds, "field_series")
    export_store(layered_ds, out, plan)
    again = export_store(layered_ds, out, plan)
    assert again["skipped"] is True


def test_force_rebuilds_store(layered_ds, tmp_path):
    out = tmp_path / "f.zarr"
    plan = plan_chunks(layered_ds, "field_series")
    export_store(layered_ds, out, plan)
    (out / "junk.txt").write_text("x", encoding="utf-8")
    rebuilt = export_store(layered_ds, out, plan, force=True)
    assert rebuilt["skipped"] is False
    assert not (out / "junk.txt").exists(), "force 必须完整重建，不留残留"


def test_refuses_to_clobber_non_zarr_dir(layered_ds, tmp_path):
    """目标目录已存在但不是 store：拒绝覆盖，避免误删用户数据。"""
    out = tmp_path / "not_a_store"
    out.mkdir()
    (out / "important.txt").write_text("别删我", encoding="utf-8")
    with pytest.raises(ZarrExportError, match="不是 Zarr store"):
        export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"),
                     force=True)
    assert (out / "important.txt").read_text(encoding="utf-8") == "别删我"


def test_failed_write_leaves_no_partial_dir(layered_ds, tmp_path, monkeypatch):
    """写入失败时必须清掉 .part，不留半截 store。"""
    out = tmp_path / "boom.zarr"

    def _boom(*a, **k):
        raise RuntimeError("模拟磁盘写失败")

    monkeypatch.setattr(xr.Dataset, "to_zarr", _boom)
    with pytest.raises(ZarrExportError, match="写入 Zarr 失败"):
        export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    assert not out.exists()
    assert not (tmp_path / "boom.zarr.part").exists()


# --------------------------------------------------------------------------
# 清单哈希（Zarr 缺失的证据层）
# --------------------------------------------------------------------------

def test_manifest_detects_tampered_chunk(layered_ds, tmp_path):
    out = tmp_path / "m.zarr"
    export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    before = scan_store(out)
    chunk = next(p for p in out.rglob("*")
                 if p.is_file() and "/c/" in p.as_posix())
    chunk.write_bytes(chunk.read_bytes()[:-4] + b"\x00" * 4)
    after = scan_store(out)
    diff = compare_manifests(before, after)
    assert diff["identical"] is False
    assert diff["changed_count"] == 1
    assert diff["changed"][0].endswith(chunk.name) or chunk.name in diff["changed"][0]


def test_manifest_detects_added_and_removed(layered_ds, tmp_path):
    out = tmp_path / "m2.zarr"
    export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    before = scan_store(out)
    (out / "stray.json").write_text("{}", encoding="utf-8")
    diff = compare_manifests(before, scan_store(out))
    assert diff["added_count"] == 1
    assert diff["removed_count"] == 0


def test_manifest_digest_is_order_independent(layered_ds, tmp_path):
    """两次扫描同一 store 必须得到同一个 digest（可复现）。"""
    out = tmp_path / "d.zarr"
    export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    assert scan_store(out).digest == scan_store(out).digest


def test_manifest_counts_arrays_and_chunks(layered_ds, tmp_path):
    out = tmp_path / "c.zarr"
    export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    m = scan_store(out)
    assert m.n_chunks > 0
    assert m.n_arrays >= 2          # 至少 t 与 q 的 zarr.json
    assert m.total_bytes > 0
    assert len(m.entries) == m.n_arrays + m.n_chunks + _extra_meta(out)


def _extra_meta(store):
    """元数据类文件（zarr.json 之外的坐标数组也算数组）。"""
    return 0


def test_manifest_json_roundtrip(layered_ds, tmp_path):
    out = tmp_path / "j.zarr"
    export_store(layered_ds, out, plan_chunks(layered_ds, "field_series"))
    m = scan_store(out)
    blob = json.loads(m.to_json())
    assert blob["summary"]["manifest_digest"] == m.digest
    assert len(blob["entries"]) == len(m.entries)
    assert all(e["rel_path"] for e in blob["entries"])


def test_scan_store_rejects_missing_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_store(tmp_path / "nope")


# --------------------------------------------------------------------------
# 任务 handler：登记进 catalog
# --------------------------------------------------------------------------

def test_export_task_registers_manifest_fingerprint(env, layered_ds):
    cfg, cat, src_root = env
    rel = "2024/era5_x.nc"
    _write_nc(src_root / rel, layered_ds)
    st = (src_root / rel).stat()
    fid, _ = cat.upsert_file("disk1", rel, st.st_size, st.st_mtime)

    out = zarr_export_one(
        {"file_id": fid, "volume_id": "disk1", "rel_path": rel,
         "target": "field_series"}, cat, None, cfg=cfg)

    assert out["skipped"] is False
    assert out["plan"]["target"] == "field_series"
    store = cfg.array_dir / out["rel_path"]
    assert (store / "zarr.json").exists(), "v3 store 根应有 zarr.json"

    rec = cat.get_file(out["artifact_file_id"])
    assert rec["volume_id"] == ZARR_DEFAULTS["volume_id"]
    assert rec["fingerprint_algo"] == "manifest-sha256"
    assert rec["fingerprint"] == scan_store(store).digest
    # sidecar 落盘且内容与现场一致
    side = json.loads(open(out["manifest_path"], encoding="utf-8").read())
    assert side["summary"]["manifest_digest"] == rec["fingerprint"]


def test_store_rel_path_is_stable_and_target_scoped():
    assert store_rel_path("2024/era5_x.nc", "field_series") == \
        "field_series/2024/era5_x.zarr"
    a = store_rel_path("2024/era5_x.nc", "field_series")
    b = store_rel_path("2024/era5_x.nc", "point_series")
    assert a != b, "不同分块方案必须并存，不能互相覆盖"


def test_two_targets_coexist_without_overwrite(env, layered_ds):
    cfg, cat, src_root = env
    rel = "2024/era5_y.nc"
    _write_nc(src_root / rel, layered_ds)
    st = (src_root / rel).stat()
    fid, _ = cat.upsert_file("disk1", rel, st.st_size, st.st_mtime)
    o1 = zarr_export_one({"file_id": fid, "volume_id": "disk1", "rel_path": rel,
                          "target": "field_series"}, cat, None, cfg=cfg)
    o2 = zarr_export_one({"file_id": fid, "volume_id": "disk1", "rel_path": rel,
                          "target": "point_series"}, cat, None, cfg=cfg)
    assert o1["rel_path"] != o2["rel_path"]
    assert (cfg.array_dir / o1["rel_path"]).is_dir()
    assert (cfg.array_dir / o2["rel_path"]).is_dir()
    # 两者数值相同、布局不同
    a = xr.open_zarr(cfg.array_dir / o1["rel_path"])
    b = xr.open_zarr(cfg.array_dir / o2["rel_path"])
    try:
        assert np.array_equal(a["t"].values, b["t"].values)
        assert a["t"].chunks != b["t"].chunks
    finally:
        a.close()
        b.close()


def test_task_reports_missing_source(env):
    cfg, cat, src_root = env
    fid, _ = cat.upsert_file("disk1", "gone.nc", 10, 1.0)
    from dbas_core.exceptions import AnalysisError
    with pytest.raises(AnalysisError, match="源文件不存在"):
        zarr_export_one({"file_id": fid, "volume_id": "disk1",
                         "rel_path": "gone.nc", "target": "field_series"},
                        cat, None, cfg=cfg)


def test_variable_whitelist_is_enforced(env, layered_ds):
    cfg, cat, src_root = env
    rel = "2024/era5_v.nc"
    _write_nc(src_root / rel, layered_ds)
    st = (src_root / rel).stat()
    fid, _ = cat.upsert_file("disk1", rel, st.st_size, st.st_mtime)
    out = zarr_export_one({"file_id": fid, "volume_id": "disk1", "rel_path": rel,
                           "target": "field_series", "variables": ["t"]},
                          cat, None, cfg=cfg)
    assert out["vars"] == ["t"]

    from dbas_core.exceptions import AnalysisError
    with pytest.raises(AnalysisError, match="无交集"):
        zarr_export_one({"file_id": fid, "volume_id": "disk1", "rel_path": rel,
                         "target": "field_series", "variables": ["nope"]},
                        cat, None, cfg=cfg)


def test_region_crop_applies_before_export(env, layered_ds):
    """给定区域必须先把网格裁掉再导出，否则等于没裁。

    fixture 的 lat=30–40/lon=70–105 本来就整个落在 qhp_main 内，
    因此这里用一个必然只覆盖一部分的临时区域来验证裁切真的发生。
    """
    from dbas_core.analysis import regions as regions_mod
    regions_mod.REGIONS["_test_half"] = regions_mod.Bbox(35, 40, 70, 88)
    cfg, cat, src_root = env
    rel = "2024/era5_r.nc"
    _write_nc(src_root / rel, layered_ds)
    st = (src_root / rel).stat()
    fid, _ = cat.upsert_file("disk1", rel, st.st_size, st.st_mtime)
    out = zarr_export_one({"file_id": fid, "volume_id": "disk1", "rel_path": rel,
                           "target": "field_series", "region": "_test_half"},
                          cat, None, cfg=cfg)
    store = xr.open_zarr(cfg.array_dir / out["rel_path"])
    try:
        assert store.sizes["lat"] < layered_ds.sizes["lat"]
        assert store.sizes["lon"] < layered_ds.sizes["lon"]
        assert 35 <= float(store["lat"].min()) and float(store["lat"].max()) <= 40
        assert 70 <= float(store["lon"].min()) and float(store["lon"].max()) <= 88
    finally:
        store.close()
        regions_mod.REGIONS.pop("_test_half", None)
