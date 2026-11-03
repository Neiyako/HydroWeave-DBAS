# 数组层设计：文件级证据 + Zarr 切片，两者混用

状态：已实现（2026-11-03）。实现 `src/dbas/zarr_export/`，测试 `tests/test_zarr_export.py`。

## 一、为什么混用而不是二选一

DBAS 原来的资产单位是**文件**：`volume_id + rel_path` 定位、逐文件 sha256、逐文件 QC。
Zarr 的资产单位是**数组**：一个 store 摊成成百上千个 chunk，切片快、并行好，
但失去“这个资产是什么、是否被改过”的回答能力。

公开的 ERA5→Zarr 数据集（[google-research/arco-era5](https://github.com/google-research/arco-era5)）
选择了另一条路：把分块常量写死在数据集里。它的 AR 版本用
`{time:1, latitude:721, longitude:1440, level:37}`（单变量 154 MB/chunk），
为“整场单时刻”优化。照抄它会有一个直接后果：**单点时间序列查询会慢一个数量级**。

本仓的选择是——

| 层 | 职责 | 存储单位 | 证据 |
|---|---|---|---|
| 原始卷 | 唯一事实来源，只读 | 单文件 `.nc` | 逐文件 sha256、R001–R105 QC |
| 分析卷（subset） | 区域裁切 ARD | 单文件 `.nc` | 文件内校验 + QC |
| **数组层（zarr_export）** | 可切片、可并行 | 一个 store 目录 | **清单哈希**（manifest digest） |

原始数据一个字节都不动；数组层随时可删可重建。

## 二、核心设计：分块是查询模式的函数

这是与公开数据集最本质的分歧。分块不是“越大越好”或“照抄论文”，
取决于你要怎么查。四种内置模式：

| target | 典型查询 | time | 空间 | level |
|---|---|---|---|---|
| `field_series` | 某时刻全球/区域场 | 1 | 整块 | 全部 |
| `point_series` | 单点/少数站点的长序列 | 整段 | 8×8 | 全部 |
| `area_stats` | 区域平均、时间统计 | 128 | 32 | 全部 |
| `level_profile` | 按气压层做垂直剖面 | 24 | 整块 | 1 |

**实测数据**（426 MB、240×37×100×120 的合成 ERA5 形态数据，本机 macOS）：

| 查询 | `field_series` 布局 | `point_series` 布局 | 结论 |
|---|---|---|---|
| 单点整段时间序列 | 225.1 ms | **9.6 ms** | 布局选错慢 23.4× |
| 单时刻整场 | **8.5 ms** | 188.8 ms | 方向完全相反，慢 22.3× |

两种布局取回的数据**逐元素完全相同**，差别只在 I/O 放大。
所以 `zarr enqueue --target` 是必填语义的配置，不是可有可无的调优项。

分块超出元素预算（默认 8M 元素 ≈ 32 MB f4）时按 **时间 → level → 空间** 的顺序退让：
时间维变碎只是变慢，空间维变碎会破坏“整场”语义，所以放最后。

## 三、补上 Zarr 最缺的东西：比特级证据

Zarr 没有“单个文件”这个概念，`checksum` 无处施加。本仓用**清单哈希**：

```
digest = sha256( 每行 "相对路径\tsize\tmtime_ns\tsha256\n" 按路径排序拼接 )
```

- 保留逐 chunk 的 sha256，因此能定位是**哪个** chunk 变了；
- 汇总成一个 digest 写进 `files.fingerprint`（`algo = manifest-sha256`），
  沿用 DBAS 既有指纹语义，`dbas zarr verify` 直接对账；
- 清单全文落 `data/reports/zarr/<file_id>.manifest.json`，可离线查证。

实测：篡改一个 4 字节的 chunk 后，`zarr verify` 报
`✘ 不一致`，并精确列出 `changed: t/c/20/0/0/0`。

## 四、命令

```bash
# 1) 预览：同一份数据在四种查询模式下会得到什么布局（不写数据）
python -m dbas zarr plan --source path/to/ERA5_025dg_20240101_t.nc

# 2) 入队：可以同时给同一份数据建多种布局，互不覆盖
python -m dbas zarr enqueue --volume disk1 --target field_series,point_series

# 3) 执行：分批 / 预算 / 断点续跑，语义与 scan、subset 完全一致
python -m dbas zarr run --budget 900 --batch-size 5

# 4) 对账与查看
python -m dbas zarr info
python -m dbas zarr verify --store field_series/2024/ERA5_x.zarr
```

产物路径：`data/arrays/<target>/<源相对路径去后缀>.zarr`。
按 target 分目录，是为了让同源数据的多种布局并存、可对比实测。

## 五、边界与已知限制

1. **不替代原始卷**。数组层是派生物，删掉不影响任何既有能力。
2. **store 目录作为“一个条目”登记**。它的大小/指纹是目录 stat 与清单哈希，
   不是单文件 sha256——`dbas checksum` 不会去逐 chunk 计算，用 `zarr verify` 代替。
3. **分片（shards）默认关闭**。开启能显著减少文件数（移动硬盘/网盘同步友好），
   但会让“改了一个 chunk”的粒度变成“改了一个分片”。按存储介质与备份方式选。
4. **zarr v3 的 consolidated metadata 仍是实验特性**，导入时会出现
   `ZarrUserWarning`，不影响读写；如介意可把 `zarr.consolidate` 设为 false。
5. **`clobber` 保护**：目标目录存在但不是 Zarr store 时拒绝覆盖，
   避免把用户目录当产物误删。
6. **未做的部分**：跨文件合并成单一时间轴 store（现在的粒度是一源文件一 store）。
   真要“1980–2024 一条时间轴”，应在导出后单独做一次 consolidate，
   或等 dbas-core 提供 append 语义——这是下一步的工作，不在本实现范围内。

## 六、外部参照

- [google-research/arco-era5](https://github.com/google-research/arco-era5)：
  AR/CO 双轨（analysis-ready 与 cloud-optimized）、分块常量、以及
  “grib→zarr 不做任何其它修改”的取舍。本仓借鉴其**双轨思想**（原始 vs 派生），
  但**不照抄其分块常量**——理由见第二节实测。
- 另有 Pangeo Forge（完整 ERA5 镜像方向）、`era5-land-zarr-pipeline`
  等仓库，粗读定位后未采用。
