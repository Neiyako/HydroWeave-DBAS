# HydroWeave / DBAS — 科研数据资产数据库管理系统

DBAS（以数据库为核心的数据资产管理系统）面向个人与小团队的科研数据资产管理：把分散在移动硬盘、工作目录和下载目录中的文件，登记为**可检索、可校验、可追踪、可恢复的本地目录数据库**。

> **DBAS 管理数据库，不替代原始数据仓库。** 原始卷保持只读；SQLite catalog 保存卷、文件、元数据、数据集、质量证据和任务状态；扫描、校验、备份和任务队列负责让这份目录长期可维护。

当前适配场景是 ERA5/NetCDF，但核心思路适用于任何以文件为单位的科研数据资产。

- **本仓库 HydroWeave/DBAS：管理与编排** —— 卷登记、目录索引、扫描调度、任务队列、校验、备份、下载接入和运维报告。
- **[dbas-core](https://github.com/Neiyako/dbas-core)：计算与绘图基础** —— NetCDF 解析、数据集识别、画像/QC、区域裁切、物理量计算，以及为 xarray/Matplotlib 绘图准备可审计结果。

当前版本 `0.1.0`。数据库管理链路、CDS 小体量 fetch、内容画像/QC 和 L1 subset 已实现；通用 EDA、清洗、特征工程和建模接口仍在迭代。

---

## 一、DBAS 管理什么

DBAS 将“文件堆”整理成一套本地资产目录：

```text
原始数据卷（只读）
        │ volume_id + rel_path
        ▼
SQLite catalog（可备份、可检查、可重建）
  volumes / files / datasets
  variables / dimensions / profiles
  quality_reports / tasks / task_attempts / scan_runs
        │
        ├── 扫描、解析、校验、画像、QC 任务
        ├── Markdown 盘点与质量报告
        └── analysis / downloads 等派生卷
```

核心管理语义：

- **稳定定位**：文件使用 `volume_id + 卷内相对路径`，移动硬盘更换挂载点不会改变索引身份。
- **先盘点、后解析**：`inventory` 只建立文件清单；`metadata` 再按队列读取 NetCDF header，不把 TB 级数据整体载入内存。
- **增量维护**：按路径、大小和修改时间识别新增/变化文件，重复运行不会重复创建任务。
- **状态可解释**：文件状态包括 `new/active/missing_pending/offline/removed`；解析状态包括 `unparsed/parsed/failed/not_netcdf`；任务状态包括 `pending/running/retry_wait/succeeded/failed`。
- **可恢复执行**：每文件任务独立提交，支持预算、批次、重试、陈旧任务回收和断点续跑。
- **原始卷不写入**：DBAS 只读访问原始卷，数据库、报告、下载和 subset 产物写入项目侧 `data/`。
- **证据优先**：解析失败、QC 命中、文件缺失和校验结果都保留状态与证据，不用猜测值覆盖未知。

## 二、核心能力

| 能力 | DBAS 提供的管理价值 | 主要命令 |
|---|---|---|
| 数据卷登记 | 为移动硬盘、下载目录和分析产物建立稳定身份 | `dbas volume register/list` |
| 目录数据库 | 维护卷、文件、数据集、变量、维度和任务之间的关系 | `dbas status`、`dbas dataset list` |
| 盘点与增量扫描 | 先清点资产，再按变化增量入队；离线卷不会被误删 | `dbas scan --mode inventory/incremental` |
| 元数据索引 | 只读解析 NetCDF header，记录时间轴、空间范围、变量和维度 | `dbas scan --mode metadata` |
| 任务编排 | 原子认领、批次/预算、重试、失败隔离、断点续跑 | `dbas tasks list/show` |
| 完整性与校验 | 文件可读性检查、流式 SHA-256 和数据库完整性检查 | `dbas verify/checksum`、`dbas db check` |
| 备份与重建 | SQLite 在线备份；原始数据不变时可从卷重新建立目录 | `dbas db backup`、`dbas db rebuild` |
| 运维报告 | 从 catalog 输出盘点、任务和 QC 报告，不重复扫描全盘 | `dbas report inventory` |

### 数据库之上的派生工作流

这些能力建立在 catalog 之上，但不是 DBAS 的首要定位：

- `dataset assign/list`：按路径规则给文件归组；
- `profile` / `qc`：对文件做抽样画像并记录质量证据；
- `subset`：使用 dbas-core 和 xarray/dask 生成区域裁切产物；
- `fetch`：管理小体量 CDS 请求，下载成功后自动登记为普通数据卷；
- 下游计算与绘图：由 [dbas-core](https://github.com/Neiyako/dbas-core) 和上层分析脚本负责。

---

## 三、30 秒建立一份数据库

以下流程使用合成 NetCDF，不联网：

```bash
.venv/bin/python scripts/make_demo_volume.py

dbas volume register data/demo_volume --id demo
dbas scan --volume demo --mode inventory --enqueue-metadata
dbas scan --volume demo --mode metadata
dbas status
dbas report inventory
dbas db check
dbas db backup
```

运行后得到：

- `data/catalog.db`：SQLite 目录数据库；
- `data/reports/`：盘点与运维报告；
- `data/backups/`：数据库备份；
- 原始 `data/demo_volume/`：仍只作为被索引的文件卷，不被 DBAS 改写。

查看任务队列和失败原因：

```bash
dbas tasks list --status pending
dbas tasks list --status failed
dbas tasks show TASK_ID
dbas tasks prune
```

## 四、接入真实数据盘

```bash
# 1. 显式指定稳定 ID；换挂载点后仍使用同一个 ID
dbas volume register /Volumes/ERA5HD --id era5hd

# 2. 先盘点，确认文件数量，再入队解析
dbas scan --volume era5hd --mode inventory
dbas scan --volume era5hd --mode inventory --enqueue-metadata

# 3. 小预算试跑；中断后重复同一命令即可续跑
dbas scan --volume era5hd --mode metadata --budget 60 --batch-size 20

# 4. 维护目录中的数据集、画像和 QC 证据
dbas dataset assign --volume era5hd
dbas profile --volume era5hd --limit 100
dbas qc run --volume era5hd
dbas qc report --volume era5hd

# 5. 进行文件与数据库维护
dbas verify --volume era5hd
dbas checksum --volume era5hd --batch-size 5
dbas db check && dbas db backup
```

### 离线卷和断点语义

- 卷不可访问时扫描直接报错，不会把文件标记为已删除。
- 只有卷在线且完整扫描成功后仍未发现的文件，才会进入 `missing_pending`。
- 卷重新挂载后用同一 `volume_id` 注册，文件状态可在下一次扫描恢复。
- 预算、批次、资源水位或进程中断都可以通过重跑命令恢复；`running` 任务会被回收为 `pending`。

详细安全边界见 [docs/architecture.md](docs/architecture.md) 和 [docs/operations.md](docs/operations.md)。

## 五、CDS 下载如何进入数据库链路

CDS fetch 是数据库管理的接入适配器，不是独立的数据分析器。每个请求按变量集、年份和月份建立幂等任务；成功下载后写入 `downloads` 卷，并自动入队 metadata 解析。

```bash
dbas fetch plan --config configs/fetch/era5_demo.yaml
dbas fetch enqueue --config configs/fetch/era5_demo.yaml
dbas fetch run --config configs/fetch/era5_demo.yaml
```

因此下载文件与外接硬盘文件共享同一套 `inventory → metadata → verify → checksum → report` 管理流程。抓包分析为可选的脱敏诊断工具，详见 `configs/fetch/` 和 `scripts/`。

## 六、两仓职责

| 层 | HydroWeave / DBAS | dbas-core |
|---|---|---|
| 主问题 | 数据资产如何登记、检索、维护和恢复 | 数据如何解析、计算、质控和变成可绘图结果 |
| 数据卷 | 注册、在线/离线状态、稳定路径 | 通过 catalog 使用索引结果 |
| 扫描任务 | inventory、incremental、metadata、verify、checksum | 提供 header 解析和任务处理内核 |
| 数据理解 | 调度 dataset/profile/QC 工作流 | 数据集识别、抽样画像、QC 规则 |
| 派生数据 | subset/fetch 任务编排和产物登记 | 区域裁切、坐标归一化、物理量计算 |
| 输出 | catalog、任务历史、运维报告 | 可审计数组/表格结果，供 xarray/Matplotlib/Cartopy 绘图 |
| 不负责 | 不把自己包装成通用绘图库 | 不负责移动硬盘管理、全盘扫描和下载调度 |

## 七、下游计算与绘图案例

DBAS 的作用是把原始卷整理成可定位、可复核的数据资产；真正的物理计算和绘图放在 dbas-core 与上层脚本中。当前仓库保留一个真实案例作为“管理链路的下游结果”，而不是产品首页：

- 2010–2013 年青藏高原 ERA5 气压层数据；
- dbas-core 计算 PW、RH500 和云发生频率代理；
- 上层脚本使用 Matplotlib/xarray 生成时间序列、空间分布和垂直剖面图。

![年际对比](docs/img/fig_annual_comparison.png)

> 当前案例的原始卷只有 `t/q/u/v`，PW/RH500/CFRAC 是代理指标，不是真云水含量；真云水需要 `clwc/ciwc` 或总云量场。完整计算口径见 [dbas-core](https://github.com/Neiyako/dbas-core) 的 `cloud_water_proxy` 模块。

## 八、架构与目录

```text
cli
 ├── volume / scan / verify / checksum ──> catalog
 ├── tasks / scheduler / resource_guard ─> catalog
 ├── fetch ─────────────────────────────> catalog + downloads volume
 └── report ────────────────────────────> catalog
                                             │
                                             ▼
                                  dbas-core inspection / analysis / qc
```

```text
src/dbas/
├── storage/         卷身份识别与挂载辅助
├── scanner/         inventory / incremental / metadata / verify / checksum
├── scheduler/       原子认领、预算、重试与陈旧任务回收
├── resource_guard/  RSS/可用内存软保护
├── fetch/           ERA5 CDS API 下载与捕获诊断
├── reports/         Markdown 盘点与 QC 报告
└── cli.py           统一命令入口

data/                catalog、备份、报告和派生产物（不入 Git）
tests/               合成数据测试（默认不联网）
docs/                 架构说明、运行手册与例图
```

底层包的主要模块包括：`dbas_core.catalog`、`dbas_core.inspection`、`dbas_core.analysis.datasets`、`profile`、`regions`、`subset`、`cloud_water_proxy` 和 `dbas_core.qc`。

## 九、安装与测试

需要 Python 3.10+。安装 DBAS 时会自动从 GitHub 拉取 dbas-core：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest

dbas --help
```

可选依赖：

```bash
.venv/bin/python -m pip install -e '.[fetch]'   # CDS API
.venv/bin/python -m pip install -e '.[eda]'     # subset / 画像 / 实验性 EDA
```

测试使用临时目录和合成 NetCDF，不联网、不依赖真实 ERA5 卷，覆盖 CLI、扫描、任务调度、fetch 管理链路和 dbas-core 的解析/画像/QC/subset 测试。

## 十、路线图与边界

已实现：

- SQLite catalog、卷注册、inventory/incremental、元数据解析任务；
- 预算、批次、重试、陈旧回收、断点续跑；
- verify/checksum、数据库检查、在线备份和 Markdown 报告；
- CDS 小体量下载与 catalog 集成；
- 首版内容画像、QC 证据和区域裁切。

未实现或仍在迭代：

- 通用 EDA、非破坏性清洗、特征工程、Parquet 导出和建模接口；
- 更完整的 QC 规则、人工复核和危险解析隔离；
- 通用气候态、季节循环、海拔分层统计与绘图 API。

DBAS 的主产品边界是**数据库管理与任务编排**；计算公式、分析结果和绘图工作流请使用 [dbas-core](https://github.com/Neiyako/dbas-core) 及其上层分析脚本。

## 许可

见 [LICENSE](LICENSE)。
