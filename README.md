# DBAS — 科研数据资产管理与任务编排系统

把分散在移动硬盘、工作目录和下载目录里的科研数据，登记成**可检索、可校验、可追踪、可恢复**的本地目录数据库，并在其上提供可切片的数组层。

> **DBAS 管理数据资产，不替代原始数据仓库。** 原始卷保持只读；SQLite catalog 保存卷、文件、元数据、数据集、质量证据和任务状态；扫描、校验、备份、任务队列让这份目录长期可维护。

当前版本 `0.1.0`，适配 ERA5/NetCDF，核心机制适用于任何以文件为单位的科研数据资产。

---

## 一、它解决什么问题

科研数据散在移动硬盘和下载目录里，常见症状：

- TB 级数据不知道有什么、缺没缺、坏没坏；
- 移动硬盘换台机器挂载点就变，脚本里的绝对路径全失效；
- 想查某个变量某段时间的数据，只能整文件打开，一个查询要等几十秒；
- 谁改了哪个文件说不清，没有比特级证据。

DBAS 的对应回答：

| 症状 | DBAS 的处理 | 命令 |
|---|---|---|
| 不知道有什么 | 盘点成清单，按数据集/变量/时间/空间检索 | `scan --mode inventory`、`dataset list` |
| 换机器路径失效 | 稳定身份 = `volume_id` + 卷内相对路径 | `volume register --id` |
| 文件坏没坏 | 可读性检查 + 流式 SHA-256 | `verify`、`checksum` |
| 查询慢 | 按查询模式分块的 Zarr 数组层 | `zarr plan/enqueue/run` |
| 改没改过说不清 | 逐 chunk 清单哈希 | `zarr verify` |
| 数据库本身会坏 | 在线备份 + 完整性检查 | `db backup`、`db check` |

## 二、两条独立的证据链

这是本项目的核心设计，也是与"直接上一个 Zarr 数据集"的关键分歧。

| 层 | 存储单位 | 回答的问题 | 证据方式 |
|---|---|---|---|
| **原始卷 / 分析卷** | 单个 `.nc` 文件 | 这份数据是什么、有没有坏、能不能复现 | 逐文件 SHA-256 + QC 规则 |
| **数组层（Zarr）** | 一个 store 目录 | 某个时间/空间窗口的数据能多快取出来 | 清单哈希（manifest digest） |

原始数据一个字节都不动；数组层是派生物，随时可删可重建。

### 分块是查询模式的函数，不是文件大小的函数

公开的 ERA5→Zarr 数据集（如 [arco-era5](https://github.com/google-research/arco-era5)）把分块常量写死为"整场单时刻"优化。照抄它，"查单个格点的长序列"会慢一个数量级。DBAS 要求显式声明查询模式，再反推分块：

| target | 典型查询 | time | 空间 | level |
|---|---|---|---|---|
| `field_series` | 某时刻全球/区域场 | 1 | 整块 | 全部 |
| `point_series` | 单点/少数站点的长序列 | 整段 | 8×8 | 全部 |
| `area_stats` | 区域平均、时间统计 | 128 | 32 | 全部 |
| `level_profile` | 按气压层做垂直剖面 | 24 | 整块 | 1 |

同一份数据可以同时建多种布局，互不覆盖（产物按 target 分目录）。

## 三、实测性能（本机 macOS，2026-11-03）

数据：426 MB 合成 ERA5 形态卷，`240 时次 × 37 层 × 100 × 120`（f4）。取 5 次最小耗时。

| 查询 | NetCDF 原文件 | `field_series` | `point_series` |
|---|---|---|---|
| 单点长时间序列 | 28.9 ms | 215.3 ms | **3.7 ms** |
| 单时刻整场 | **3.1 ms** | 3.8 ms | 191.1 ms |

结论（不含宣传词）：

- **选对布局**：单点序列 `point_series` 比 `field_series` 快 **58×**，比直接读 NetCDF 快 **7.9×**；
- **选错布局就是灾难**：同样数据，整场查询在 `point_series` 下慢 **50×**；
- **Zarr 不是万灵药**：整场单时刻查询，`field_series` 的 3.8 ms 仍略慢于 NetCDF 的 3.1 ms。
  一次读整场的顺序 I/O 本来就是 NetCDF 的强项，Zarr 的收益在**随机切片**和**多查询并发**；
- 两种布局回读的数据与源**逐元素完全相同**（`np.array_equal` 全部 True）。

**所以什么时候值得建数组层**：需要反复按点/按区域/按层切片查询时。只整体读一遍的场景，直接用 NetCDF 更好。

## 四、快速开始（3 分钟，不联网）

```bash
git clone https://github.com/Neiyako/HydroWeave-DBAS.git
cd HydroWeave-DBAS
./scripts/setup.sh          # 一键建 venv + 装依赖，详见部署文档
./bin/dbas --help
```

生成合成数据、建立目录数据库、跑通派生链路：

```bash
.venv/bin/python scripts/demo/make_demo_volume.py
./bin/dbas volume register data/demo_volume --id demo
./bin/dbas scan --volume demo --mode inventory --enqueue-metadata
./bin/dbas scan --volume demo --mode metadata
./bin/dbas status
./bin/dbas report inventory
./bin/dbas db check && ./bin/dbas db backup
```

> **注意命令前缀**：一律用 `./bin/dbas`。原因见部署文档「为什么不用 `python -m dbas`」。

接着生成一层 Zarr 数组层并验证证据链：

```bash
./bin/dbas zarr enqueue --volume demo --target point_series
./bin/dbas zarr run
./bin/dbas zarr info
./bin/dbas zarr verify --store point_series/2023/era5_demo_00.zarr
```

`verify` 输出 `✔ 一致` 表示 store 未被改动。手动改一个 chunk 再跑，会报 `✘ 不一致` 并指出具体是哪个 chunk。

## 五、接入真实数据盘

```bash
# 1. 显式 ID：换挂载点后仍用同一个 ID，索引身份不变
./bin/dbas volume register /Volumes/ERA5HD --id era5hd

# 2. 一次扫描同时完成盘点与解析入队（新增/变化的文件才入队）
./bin/dbas scan --volume era5hd --mode inventory --enqueue-metadata

# 3. 小预算试跑，确认速率后再放开；中断后重跑同一命令即可续跑
./bin/dbas scan --volume era5hd --mode metadata --budget 600 --batch-size 50

# 4. 内容理解与质量证据
./bin/dbas dataset assign --volume era5hd
./bin/dbas profile --volume era5hd --limit 100
./bin/dbas qc run --volume era5hd

# 5. 派生数组层（按你的查询模式选 target）
./bin/dbas zarr enqueue --volume era5hd --target point_series
./bin/dbas zarr run --budget 900 --batch-size 5

# 6. 维护
./bin/dbas verify --volume era5hd
./bin/dbas db check && ./bin/dbas db backup
```

**关于 `--enqueue-metadata` 的顺序坑**：它只对"本次扫描中新增或变化"的文件入队。如果先跑了一次不带该标志的 `inventory`，文件已登记且未变化，第二次带标志也不会入队——此时直接 `scan --mode metadata` 解析已有文件即可，或用 `status` 确认 `unparsed` 数量。

**离线卷与断点语义**：

- 卷不可访问时直接报错，不会把文件标记为已删除；
- 只有卷在线且完整扫描成功后仍未发现的文件才进入 `missing_pending`；
- 重新挂载后用同一 `volume_id` 注册，下次扫描即可恢复；
- 预算、批次、资源水位、进程中断都能靠重跑恢复，`running` 任务会被回收为 `pending`；
- 回收阈值由 `configs/system.yaml` 的 `scheduler.stale_running_s` 控制（默认 600 秒），慢盘应上调而不是关掉。

## 六、CDS 下载

CDS fetch 是数据库管理的接入适配器：每个请求按变量集/年份/月份建立幂等任务，下载成功后写入 `downloads` 卷并自动入队元数据解析，与外接盘文件共享同一套管理流程。

```bash
./bin/dbas fetch plan    --config configs/fetch/era5_demo.yaml
./bin/dbas fetch enqueue --config configs/fetch/era5_demo.yaml
./bin/dbas fetch run     --config configs/fetch/era5_demo.yaml
```

需要 `.[fetch]` 附加依赖和 `~/.cdsapirc` 凭证（见部署文档）。四条可靠性约定：

- **去重键语义归一化**：`"2024"` 与 `["2024"]`、`["01","02"]` 与 `["02","01"]` 折叠成同一任务；
- **错误分级**：成本超限(403)、变量名错误、未接受条款属永久失败，不消耗重试次数，直接落 `failed` 并留原因；
- **记录 `cds_request_id`**：进程中断后可凭它取回已排队任务，不必重排；
- **抓包分两段**：`submit`（排队）与 `download`（传输）分别记录，能区分"队列慢"和"链路慢"。

设计依据见 [docs/prior-art-era5-fetch.md](docs/prior-art-era5-fetch.md)。

## 七、架构

```text
cli
 ├── volume / scan / verify / checksum ──> catalog
 ├── tasks / scheduler / resource_guard ─> catalog
 ├── subset ────────────────────────────> 核心库 ─────> analysis volume (.nc)
 ├── zarr_export ───────────────────────> arrays volume (.zarr) + manifest
 ├── fetch ─────────────────────────────> catalog + downloads volume
 └── report ────────────────────────────> catalog
```

```text
src/dbas/
├── storage/         卷身份识别、挂载辅助、数据库在线备份
├── scanner/         inventory / incremental / metadata / verify / checksum
├── scheduler/       原子认领、预算、重试、陈旧任务回收
├── resource_guard/  RSS/可用内存软保护
├── fetch/           ERA5 CDS 下载与抓包诊断
├── zarr_export/     数组层：分块计划、store 写入、清单哈希
├── reports/         Markdown 盘点与 QC 报告
└── cli.py           统一命令入口

scripts/demo/        合成数据卷生成器
configs/             system.yaml、fetch 模板、依赖约束快照
data/                catalog、备份、报告、派生产物（不入 Git）
tests/               合成数据测试（不联网）
docs/                architecture / operations / zarr-layer / prior-art
```

## 八、常用命令速查

| 命令 | 作用 |
|---|---|
| `dbas status` | 卷、文件、解析、画像、任务队列总览 |
| `dbas volume register/list` | 卷登记与查看 |
| `dbas scan --mode inventory\|incremental\|metadata\|verify` | 盘点 / 增量 / 解析 / 校验 |
| `dbas tasks list --status pending\|failed`、`tasks show ID`、`tasks prune` | 任务队列运维 |
| `dbas dataset list/assign` | 数据集分组与识别 |
| `dbas profile`、`dbas qc run/report` | 内容画像与质量证据 |
| `dbas subset` | 区域裁切为分析卷（核心库执行） |
| `dbas zarr plan/enqueue/run/verify/info` | 数组层派生与对账 |
| `dbas verify`、`dbas checksum` | 可读性检查、流式 SHA-256 |
| `dbas db check/backup/rebuild` | 数据库完整性、在线备份、重建 |
| `dbas report inventory` | Markdown 运维报告 |

所有命令加 `--help` 查看参数。

## 九、测试

```bash
.venv/bin/python -m pytest          # 90 passed，不联网、不依赖真实 ERA5
.venv/bin/python -m pytest -q tests/test_zarr_export.py   # 仅数组层
```

测试使用临时目录与合成 NetCDF，覆盖 CLI、扫描、调度、fetch 链路、在线备份、失败清理，以及数组层的分块/往返/清单哈希/登记/clobber 保护。

## 十、边界

**DBAS 的产品边界是数据管理与任务编排。** 计算公式、分析结果和绘图请用 [Hydroweave 实验仓](https://github.com/Neiyako/HydroWeave)；实验仓只读 catalog，不写本仓数据。

不在本仓范围：**实验与误差分析层**（云水物理量积分、可降水量、饱和水汽压、误差指标、
公式审计、绘图），以及通用 EDA、非破坏性清洗、特征工程、Parquet 导出、建模接口。

本仓保留的 `profile`（抽样内容画像：缺测率/数值范围/样本统计）与 `qc`
（坐标轴、时间轴、重复变量等文件级规则）**属于数据管理**——它们回答"这批数据能不能用"，
是目录数据库的一部分，不是科研算法。

核心库 `dbas_core`（catalog、config、inspection、qc、subset 等）已**内联在本仓
`src/dbas_core/`**，不再是外部 git 依赖，因此 `git clone` 后可直接安装。

已知限制：

- 数组层当前是**一源文件一 store**，跨文件合并成单一时间轴需额外 consolidate（见 [docs/zarr-layer.md](docs/zarr-layer.md)）；
- 真实 ERA5 卷上的 `zarr enqueue/run` 尚无用例覆盖，现有数组层证据全部来自合成数据；
- zarr v3 的 consolidated metadata 仍是实验特性，导入时有 `ZarrUserWarning`，不影响读写；
- 真实卷全量 `checksum` 基线需低峰期分批执行。

## 十一、文档

- [部署文档](docs/deployment.md) — 从零到跑通，含故障排查
- [Agent 提示词](docs/agent-prompts.md) — 把部署交给 AI Agent 时用的现成提示词
- [架构说明](docs/architecture.md) — 模块边界与数据流
- [运维手册](docs/operations.md) — 日常维护、备份恢复、踩坑记录
- [数组层设计](docs/zarr-layer.md) — 分块策略、清单哈希、性能实测
- [下载层先例对比](docs/prior-art-era5-fetch.md)

## 许可

见 [LICENSE](LICENSE)。
