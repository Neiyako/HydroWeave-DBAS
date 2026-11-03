# DBAS

**科研数据资产管理与任务编排系统** · Data Asset Management & Orchestration for Scientific Data

[![CI](https://github.com/Neiyako/HydroWeave-DBAS/actions/workflows/ci.yml/badge.svg)](https://github.com/Neiyako/HydroWeave-DBAS/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-orange)](pyproject.toml)

[English](README.en.md) · **简体中文**

---

把散落在移动硬盘、工作目录和下载目录里的科研数据（ERA5 / NetCDF），登记成**可检索、可校验、可追踪、可恢复**的本地目录数据库，并在其上提供按查询模式分块的 Zarr 数组层。

> **DBAS 管理数据资产，不替代数据仓库。** 原始卷全程只读；SQLite catalog 保存卷、文件、元数据、数据集、质量证据与任务状态；扫描、校验、备份与任务队列让这份目录长期可维护。

**本仓只做数据管理与抓取。** 实验计算、误差分析与绘图在独立的 [HydroWeave](https://github.com/Neiyako/HydroWeave) 实验仓。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心设计](#核心设计)
- [实测性能](#实测性能)
- [快速开始](#快速开始)
- [程序语言与依赖](#程序语言与依赖)
- [项目结构](#项目结构)
- [命令行一览](#命令行一览)
- [测试与持续集成](#测试与持续集成)
- [边界与已知限制](#边界与已知限制)
- [文档](#文档)
- [许可](#许可)

---

## 它解决什么问题

科研数据散在移动硬盘和下载目录里，常见症状与 DBAS 的对应处理：

| 症状 | DBAS 的处理 | 命令 |
|---|---|---|
| 不知道盘里有什么 | 盘点成清单，按数据集/变量/时间/空间检索 | `scan --mode inventory`、`dataset list` |
| 换机器后路径全失效 | 稳定身份 = `volume_id` + 卷内相对路径 | `volume register --id` |
| 文件坏了不知道 | 可读性检查 + 流式 SHA-256 | `verify`、`checksum` |
| 查询慢，只能整文件打开 | 按查询模式分块的 Zarr 数组层 | `zarr plan/enqueue/run` |
| 改了数据说不清 | 逐 chunk 清单哈希 | `zarr verify` |
| 数据库本身也会坏 | 在线备份 + 完整性检查 | `db backup`、`db check` |

## 核心设计

### 两条独立的证据链

| 层 | 存储单位 | 回答的问题 | 证据方式 |
|---|---|---|---|
| **原始卷 / 分析卷** | 单个 `.nc` 文件 | 这份数据是什么、有没有坏、能不能复现 | 逐文件 SHA-256 + QC 规则 |
| **数组层（Zarr）** | 一个 store 目录 | 某个时间/空间窗口的数据能多快取出来 | 清单哈希 manifest digest |

原始数据一个字节都不动；数组层是派生物，随时可删可重建。

### 分块是查询模式的函数

公开 ERA5→Zarr 数据集（如 [arco-era5](https://github.com/google-research/arco-era5)）把分块常量写死为"整场单时刻"优化。照抄它，"查单格点长序列"会慢一个数量级。DBAS 要求**显式声明查询模式**再反推分块：

| target | 典型查询 | time | 空间 | level |
|---|---|---|---|---|
| `field_series` | 某时刻全球/区域场 | 1 | 整块 | 全部 |
| `point_series` | 单点/少数站点长序列 | 整段 | 8×8 | 全部 |
| `area_stats` | 区域平均、时间统计 | 128 | 32 | 全部 |
| `level_profile` | 按气压层做垂直剖面 | 24 | 整块 | 1 |

同一份数据可并存多种布局，产物按 target 分目录、互不覆盖。

## 实测性能

426 MB 合成 ERA5 形态数据（`240 时次 × 37 层 × 100 × 120`，f4），5 次取最小值，macOS：

| 查询 | NetCDF 原文件 | `field_series` | `point_series` |
|---|---|---|---|
| 单点长时间序列 | 28.9 ms | 215.3 ms | **3.7 ms** |
| 单时刻整场 | **3.1 ms** | 3.8 ms | 191.1 ms |

- **选对布局**：单点序列 `point_series` 比 `field_series` 快 **58×**，比直接读 NetCDF 快 **7.9×**；
- **选错布局是灾难**：同样数据，整场查询在 `point_series` 下慢 **50×**；
- **Zarr 不是万灵药**：整场单时刻用 `field_series` 的 3.8 ms 仍略慢于 NetCDF 的 3.1 ms——
  一次读整场本来就是顺序 I/O 的强项，Zarr 的收益在**随机切片**与**多查询并发**；
- 两种布局回读数据与源**逐元素完全相同**（`np.array_equal` 全 True）。

**什么时候值得建数组层**：需要反复按点/区域/层切片时。只整体读一遍，直接用 NetCDF 更好。

## 快速开始

```bash
git clone https://github.com/Neiyako/HydroWeave-DBAS.git
cd HydroWeave-DBAS

./scripts/setup.sh --with-zarr     # 建 venv + 装依赖 + 自检
./bin/dbas --help
```

> 一律用 `./bin/dbas` 启动，不要用 `python -m dbas`。原因见 [部署文档](docs/deployment.md#四为什么必须用-bindbas)。

3 分钟跑通全链路（不联网、不碰真实数据）：

```bash
# 1) 合成数据 → 建库 → 扫描 → 解析
.venv/bin/python scripts/demo/make_demo_volume.py
./bin/dbas volume register data/demo_volume --id demo
./bin/dbas scan --volume demo --mode inventory --enqueue-metadata
./bin/dbas scan --volume demo --mode metadata
./bin/dbas status

# 2) 派生 Zarr 数组层并验证证据
./bin/dbas zarr enqueue --volume demo --target point_series
./bin/dbas zarr run
./bin/dbas zarr verify --store point_series/2023/era5_demo_00.zarr   # 预期 ✔ 一致
```

接入真实数据盘：

```bash
./bin/dbas volume register /Volumes/ERA5HD --id era5hd
./bin/dbas scan --volume era5hd --mode inventory --enqueue-metadata
./bin/dbas scan --volume era5hd --mode metadata --budget 600 --batch-size 50
./bin/dbas dataset assign --volume era5hd
./bin/dbas profile --volume era5hd --limit 100
./bin/dbas qc run --volume era5hd
./bin/dbas zarr enqueue --volume era5hd --target point_series && ./bin/dbas zarr run
./bin/dbas verify --volume era5hd && ./bin/dbas db check && ./bin/dbas db backup
```

完整步骤、预期输出与故障排查见 **[部署文档](docs/deployment.md)**。

## 程序语言与依赖

**Python ≥ 3.11**（CI 覆盖 3.11 / 3.12 / 3.13），辅以 Shell 启动器与部署脚本。

| 语言 | 用途 | 规模 |
|---|---|---|
| Python | 全部业务逻辑（CLI、扫描、调度、派生、抓取） | 约 7,500 行 |
| Shell | `bin/dbas` 启动器、`scripts/setup.sh` 部署脚本 | 约 110 行 |

运行时依赖（`pip install -e .` 自动安装）：

| 包 | 作用 |
|---|---|
| `netCDF4` | 读写 NetCDF，只读解析 header |
| `numpy` / `pandas` | 数值与表格处理 |
| `cftime` | CF 时间轴解码 |
| `PyYAML` | 配置文件解析 |
| `psutil` | 资源水位保护（RSS / 可用内存） |

可选依赖（按需安装）：

| extras | 内容 | 用于 |
|---|---|---|
| `[zarr]` | `xarray` `dask` `zarr>=3.0` | 数组层派生（推荐） |
| `[eda]` | `xarray` `dask` `flox` | `subset` 区域裁切 |
| `[fetch]` | `cdsapi` | CDS 下载 |
| `[dev]` | `pytest` `ruff` | 测试与静态检查 |

一键装全部：`./scripts/setup.sh --all`

## 项目结构

```text
HydroWeave-DBAS/
├── .github/workflows/ci.yml    CI：lint / test(3.11-3.13) / smoke / 部署可执行性
├── bin/dbas                    启动器（PYTHONPATH=src，绕开 .pth 被隐藏的问题）
├── configs/
│   ├── system.yaml             系统配置：调度预算、资源水位、扫描规则、数组层参数
│   ├── fetch/                  CDS 请求模板
│   └── constraints-py313.txt   实测通过的依赖版本快照
├── src/
│   ├── dbas/                   —— 应用层：数据管理与抓取 ——
│   │   ├── cli.py              统一命令入口（14 个子命令）
│   │   ├── scanner/            inventory / incremental / metadata / verify / checksum
│   │   ├── scheduler/          原子认领、预算、批次、重试、陈旧任务回收
│   │   ├── storage/            卷身份识别、挂载辅助、SQLite 在线备份
│   │   ├── resource_guard/     RSS / 可用内存软保护
│   │   ├── fetch/              CDS API 下载 + 脱敏抓包诊断
│   │   ├── reports/            Markdown 盘点与 QC 报告（原子写入）
│   │   └── zarr_export/        数组层：分块计划、store 写入、清单哈希
│   └── dbas_core/              —— 核心库（已内联，无需单独安装）——
│       ├── catalog/            卷/文件/任务/质量证据的 SQLite 仓储层
│       ├── config.py           配置加载与项目根定位
│       ├── inspection/         NetCDF header 只读解析
│       ├── qc/                 文件级质量规则（坐标轴、时间轴、重复变量）
│       └── analysis/           数据集识别、内容画像、区域裁切
├── scripts/
│   ├── setup.sh                一键部署（幂等）
│   ├── demo/                   合成数据卷生成器
│   └── cds_*                   抓包诊断工具（可选）
├── tests/                      158 个用例，全部合成数据、不联网
├── docs/                       architecture / deployment / operations / zarr-layer / agent-prompts
├── data/                       运行产物：catalog.db、报告、备份、数组层（不入 Git）
├── pyproject.toml              打包、依赖、pytest 与 ruff 配置
└── LICENSE                     MIT
```

**分层原则**：`dbas`（应用层）只做编排与数据管理，`dbas_core`（核心库）提供 catalog、解析、QC 与画像。核心库已内联进本仓，`git clone` 后可直接安装，不依赖任何外部 git 仓库。

### 数据流

```text
原始卷（只读）
   │ volume_id + rel_path
   ▼
SQLite catalog ── 扫描 / 解析 / 校验 / 画像 / QC
   │
   ├── subset ──────> 分析卷（.nc，区域裁切）
   ├── zarr_export ─> 数组层（.zarr，清单哈希）
   └── fetch ───────> downloads 卷（CDS 下载后自动登记）
```

## 命令行一览

| 命令 | 作用 |
|---|---|
| `dbas status` | 卷、文件、解析、画像、任务队列总览 |
| `dbas volume register/list` | 数据卷登记与查看 |
| `dbas scan --mode inventory\|incremental\|metadata\|verify` | 盘点 / 增量 / 解析 / 校验 |
| `dbas verify` / `dbas checksum` | 可读性检查 / 流式 SHA-256 |
| `dbas tasks list/show/prune` | 任务队列运维 |
| `dbas dataset list/assign` | 数据集分组与识别 |
| `dbas profile` / `dbas qc run\|report` | 内容画像 / 质量证据 |
| `dbas subset` | 区域裁切为分析卷 |
| `dbas zarr plan/enqueue/run/verify/info` | 数组层派生与清单对账 |
| `dbas db check/backup/rebuild` | 数据库完整性 / 在线备份 / 重建 |
| `dbas report inventory` | Markdown 运维报告 |
| `dbas fetch plan/enqueue/run` | CDS 下载管理 |

所有命令均支持 `--help`。

## 测试与持续集成

```bash
.venv/bin/python -m pytest --cov    # 158 passed，覆盖率门槛 80%
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy            # 类型检查（仅 src）
```

测试全部使用临时目录与合成 NetCDF，**不联网、不需要真实 ERA5 卷**，覆盖 CLI、扫描、调度、fetch 链路、在线备份与失败清理，以及数组层的分块、往返一致性、清单哈希、catalog 登记与 clobber 保护。

质量门禁（CI 与本地同款，任一项不达标即失败）：

| 检查 | 工具 | 门槛 | 当前实测 |
|---|---|---|---|
| 静态检查 | `ruff` | 0 问题 | 通过 |
| 类型检查 | `mypy` | 0 错误（`src`，40 个文件） | 通过 |
| 测试覆盖率 | `pytest-cov` | **≥ 80%**（含分支） | 81.6% |

`ruff` 规则集刻意保守（只查缺陷不查风格），避免因格式偏好长期变红；
覆盖率门槛写死在 `pyproject.toml` 的 `fail_under`，不是"建议值"。

CI 五个 job：

- **Lint**：`ruff`；
- **Type check**：`mypy`；
- **Test**：Python 3.11 / 3.12 / 3.13 矩阵，带 80% 覆盖率门槛；
- **Smoke**：按部署文档安装后跑真实端到端，并用 SQLite 断言解析结果确实写入，防止"退出码 0 但什么都没做"的假绿；
- **部署文档可执行性**：`setup.sh` 一键部署后跑全量测试。

## 边界与已知限制

**不在本仓范围**：实验与误差分析层（云水物理量积分、可降水量、饱和水汽压、误差指标、公式审计、绘图），以及通用 EDA、清洗、特征工程、Parquet 导出、建模接口。

保留的 `profile`（抽样画像：缺测率/数值范围/样本统计）与 `qc`（坐标轴、时间轴、重复变量等文件级规则）**属于数据管理**——它们回答"这批数据能不能用"，不是科研算法。

已知限制：

- 数组层当前是**一源文件一 store**，跨文件合并为单一时间轴需额外 consolidate；
- 真实 ERA5 卷上的 `zarr enqueue/run` 尚无用例覆盖，现有数组层证据全部来自合成数据；
- zarr v3 的 consolidated metadata 仍是实验特性，导入时有 `ZarrUserWarning`，不影响读写；
- 真实卷全量 `checksum` 基线需低峰期分批执行。

## 文档

| 文档 | 内容 |
|---|---|
| [部署文档](docs/deployment.md) | 从零到跑通，含预期输出与故障排查 |
| [Agent 提示词](docs/agent-prompts.md) | 交给 AI Agent 部署时用的现成提示词 |
| [架构说明](docs/architecture.md) | 模块边界与数据流 |
| [运维手册](docs/operations.md) | 日常维护、备份恢复、踩坑记录 |
| [数组层设计](docs/zarr-layer.md) | 分块策略、清单哈希、性能实测 |
| [下载层先例对比](docs/prior-art-era5-fetch.md) | 与 cdsapi / cdsswarm 的取舍 |

## 许可

[MIT](LICENSE) © 2026 Neiyako
