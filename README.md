# DBAS — 科研数据资产管理与任务编排系统

DBAS 面向个人与小团队的科研数据资产管理：把分散在移动硬盘、工作目录和下载目录中的文件，登记为**可检索、可校验、可追踪、可恢复的本地目录数据库**。

> **DBAS 管理数据库，不替代原始数据仓库。** 原始卷保持只读；SQLite catalog 保存卷、文件、元数据、数据集、质量证据和任务状态；扫描、校验、备份和任务队列负责让这份目录长期可维护。

当前适配场景是 ERA5/NetCDF，但核心思路适用于任何以文件为单位的科研数据资产。

## 边界：DBAS 做到哪里为止

| | DBAS（本仓） | Hydroweave 实验仓 |
|---|---|---|
| 主问题 | 数据资产如何登记、检索、维护和恢复 | 数据如何计算、质控并变成可复现图件 |
| 数据卷 | 注册、在线/离线状态、稳定路径 | 只读使用 catalog 索引结果 |
| 扫描任务 | inventory、incremental、metadata、verify、checksum | 不负责扫描 |
| 输出 | catalog、任务历史、运维报告 | run 目录、运行清单、图件与摘要 |
| 不负责 | 不做科研计算，不做绘图库 | 不做移动硬盘管理、全盘扫描和下载调度 |

**本仓不含任何实验代码、物理量计算或绘图脚本。** 科研实验与出图请使用 Hydroweave 实验仓；
需要比特级完整性证据时，由本仓 `dbas checksum` 生成，实验仓只引用结果。

当前版本 `0.1.0`。数据库管理链路、CDS 小体量 fetch、内容画像/QC 和 L1 subset 已实现；
通用 EDA、清洗、特征工程和建模接口不在本仓范围。

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

### 数据库之上的应用层

这些能力建立在 catalog 之上，用于让数据可理解，但不涉及科研计算：

- `dataset assign/list`：按路径规则给文件归组；
- `profile` / `qc`：对文件做抽样画像并记录质量证据；
- `subset`：生成区域裁切产物并登记为新卷（由 dbas-core 执行裁切）；
- `fetch`：管理小体量 CDS 请求，下载成功后自动登记为普通数据卷。

## 三、30 秒建立一份数据库

以下流程使用合成 NetCDF，不联网：

```bash
.venv/bin/python scripts/demo/make_demo_volume.py

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
- 回收阈值由 `configs/system.yaml` 的 `scheduler.stale_running_s` 控制（默认 600 秒）。
  只有超过该时长没有心跳的 `running` 任务才会被回收，避免误杀仍在解析大文件的进程；
  慢盘或大文件场景按实测解析时长上调，而不是直接关掉回收。

详细安全边界见 [docs/architecture.md](docs/architecture.md) 和 [docs/operations.md](docs/operations.md)。

## 五、CDS 下载如何进入数据库链路

CDS fetch 是数据库管理的接入适配器，不是独立的数据分析器。每个请求按变量集、年份和月份建立幂等任务；成功下载后写入 `downloads` 卷，并自动入队 metadata 解析。

```bash
dbas fetch plan --config configs/fetch/era5_demo.yaml
dbas fetch enqueue --config configs/fetch/era5_demo.yaml
dbas fetch run --config configs/fetch/era5_demo.yaml
```

因此下载文件与外接硬盘文件共享同一套 `inventory → metadata → verify → checksum → report` 管理流程。抓包分析为可选的脱敏诊断工具，详见 `configs/fetch/` 和 `scripts/`。

### 下载可靠性约定

借鉴 `ecmwf/cdsapi`、`cdsswarm` 等既有实现后，fetch 层固化了四条约定：

- **去重键按语义归一化**：`"year": "2024"` 与 `["2024"]`、`["01","02"]` 与 `["02","01"]` 折叠成同一个 `dedup_key`，避免同一份数据被重复入队；`area` 是位置参数，保持原序不参与排序。
- **错误分级**：成本超限（403）、变量名写错、未接受数据集条款属于**永久失败**，直接落 `failed` 并保留原因，不消耗重试次数；网络抖动、5xx 仍按原策略重试。
- **记录 `cds_request_id`**：任务结果里带回 CDS 服务端请求 ID，进程中断后可凭它取回已排队的任务，不必重排。
- **抓包分两段**：`submit`（排队，记 `request_id` 与 `content_length`）与 `download`（传输，记实测速率与字节数）分别记录，能区分"队列慢"和"链路慢"，并用 `size_matches` 标出截断产物。

设计依据与先例对比见 [prior-art-era5-fetch.md](docs/prior-art-era5-fetch.md)。

## 六、下游科研工作如何使用这份目录

DBAS 是数据侧的**唯一入口**，下游实验仓只读 catalog：

```bash
# 实验仓（Hydroweave）通过 config.yaml 声明：
#   catalog: <本仓 data/catalog.db 路径>
#   volume_id: era5hd
#   dataset_id: era5_pressure
# 然后自己做计算、质控与制图，并写自己的 run manifest。
```

- 实验仓**不写** catalog，也不改原始卷；
- 实验产物、运行清单和验收报告都在实验仓，不进本仓 `data/`；
- 需要比特级归档证据时，用本仓 `dbas checksum` 分批生成。

## 七、架构与目录

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
├── storage/         卷身份识别、挂载辅助与数据库在线备份
├── scanner/         inventory / incremental / metadata / verify / checksum
├── scheduler/       原子认领、预算、重试与陈旧任务回收
├── resource_guard/  RSS/可用内存软保护
├── fetch/           ERA5 CDS API 下载与捕获诊断
├── reports/         Markdown 盘点与 QC 报告
└── cli.py           统一命令入口

scripts/             只放可执行入口，约定见 scripts/README.md
├── demo/            合成数据卷生成器
└── *.py             抓包诊断等运维工具

configs/             系统配置、fetch 请求模板与依赖约束文件
data/                catalog、备份、报告和派生产物（不入 Git）
tests/               合成数据测试（默认不联网）
docs/                架构说明与运行手册
```

底层包 `dbas-core` 的主要模块包括：`dbas_core.catalog`、`dbas_core.inspection`、
`dbas_core.analysis.datasets`、`profile`、`regions`、`subset` 和 `dbas_core.qc`。

## 八、安装与测试

需要 Python 3.11+。安装 DBAS 时会自动从 GitHub 拉取 dbas-core（已在
`pyproject.toml` 钉到固定 commit，避免上游漂移）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest

dbas --help
```

机器专属依赖版本记录在 `configs/constraints-py313.txt`（本机实测通过的快照）。
若 `import dbas` 报 `No module named 'dbas'`，先查可编辑安装的 `.pth` 是否被
macOS 打上隐藏位（Python 3.13 的 `site.py` 会跳过隐藏 `.pth`）：

```bash
ls -lO .venv/lib/python3.*/site-packages/*.pth | grep hidden
chflags nohidden .venv/lib/python3.*/site-packages/*.pth
./bin/dbas status          # 自带 PYTHONPATH=src，不依赖 .pth
```

可选依赖：

```bash
.venv/bin/python -m pip install -e '.[fetch]'   # CDS API
.venv/bin/python -m pip install -e '.[eda]'     # subset / 画像
```

测试使用临时目录和合成 NetCDF，不联网、不依赖真实 ERA5 卷，覆盖 CLI、扫描、
任务调度、fetch 管理链路、在线备份与失败清理、dbas-core 的解析/画像/QC/subset：

```bash
.venv/bin/python -m pytest -q
```

## 九、路线图与边界

已实现：

- SQLite catalog、卷注册、inventory/incremental、元数据解析任务；
- 预算、批次、重试、陈旧回收、断点续跑；
- verify/checksum、数据库检查、在线备份和 Markdown 报告；
- CDS 小体量下载与 catalog 集成；
- 首版内容画像、QC 证据和区域裁切。

未实现或仍在迭代：

- 通用 EDA、非破坏性清洗、特征工程、Parquet 导出和建模接口（不在本仓范围）；
- 更完整的 QC 规则、人工复核和危险解析隔离；
- 真实卷全量 `checksum` 比特级基线（需低峰期分批执行）。

**DBAS 的产品边界是数据库管理与任务编排。** 计算公式、分析结果和绘图工作流请使用
Hydroweave 实验仓与 dbas-core。

## 许可

见 [LICENSE](LICENSE)。
