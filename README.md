# DBAS — ERA5 科研数据管理与分析系统

DBAS（Data Management and Analysis System）面向个人科研工作流，提供 ERA5/NetCDF 数据的**数据卷登记、目录索引、元数据解析、任务调度、质量检查、区域裁切与 CDS 下载管理**。原始数据只读，运行状态与报告写入项目数据目录；任务支持分批、重试和断点续跑。

> 当前版本：`0.1.0`。核心管理链路、CDS fetch、内容画像/QC 和 L1 subset 已实现；更高阶 EDA 与特征工程仍在迭代中。

## 能力概览

- **目录管理**：以“卷 ID + 卷内相对路径”稳定定位文件，挂载路径变化不影响索引。
- **扫描与解析**：先 inventory，再按任务队列解析 NetCDF 头；支持增量扫描、预算、批次和断点续跑。
- **校验与运维**：文件可读性检查、流式 SHA-256、任务重试/清理、SQLite integrity check 与在线备份。
- **数据集与 QC**：按路径规则识别数据集，生成内容画像，记录时间轴、fill 值、物理范围和文件级覆盖率等 QC 证据。
- **区域裁切**：基于 xarray+dask 的惰性裁切；支持降序纬度、0–360 经度等常见 ERA5 轴约定，产物自动登记为 analysis 卷。
- **CDS 下载**：小体量请求的 plan/enqueue/run 管理；临时 `.part` 文件完成后原子改名，避免留下半截文件。
- **实验性分析**：`scripts/tp_cloud_water_eda.py` 和 `scripts/tp_cloud_water_plot.py` 提供青藏高原水汽/成云潜势代理指标分析。它们依赖具体数据命名、外部底图和本机目录，**不是真云水含量，也不是稳定 CLI**，请按脚本顶部说明调整后再使用。

## 安装

需要 Python 3.10 或更高版本：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 运行测试
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

按需安装可选依赖：

```bash
# CDS API 下载
.venv/bin/python -m pip install -e '.[fetch]'

# subset、内容画像和实验性 EDA
.venv/bin/python -m pip install -e '.[eda]'
```

安装后优先使用 console script：

```bash
dbas --help
```

如果 macOS 同步工具导致 editable 安装的 `.pth` 被忽略，可使用不依赖 `.pth` 的备用入口：

```bash
PYTHONPATH=src .venv/bin/python -m dbas --help
# 或
./bin/dbas --help
```

`bin/dbas` 是本仓库的开发辅助启动器，默认使用项目内 `.venv`；在其他环境中请使用已安装的 `dbas` 命令。

## 30 秒体验：合成数据

以下命令只创建项目内的合成 NetCDF，不触碰真实 ERA5 数据，也不联网：

```bash
.venv/bin/python scripts/make_demo_volume.py
dbas volume register data/demo_volume --id demo
dbas scan --volume demo --mode inventory --enqueue-metadata
dbas scan --volume demo --mode metadata
dbas report inventory
dbas status
```

要体验类 ERA5 全球逐日数据的区域裁切（需先安装 `.[eda]`）：

```bash
.venv/bin/python scripts/make_demo_era_volume.py data/era_like_volume 3
dbas volume register data/era_like_volume --id eralike
dbas scan --volume eralike --mode inventory --enqueue-metadata
dbas scan --volume eralike --mode metadata
dbas subset plan --volume eralike
dbas subset enqueue --volume eralike
dbas subset run
```

运行数据默认位于 `data/`，已被 `.gitignore` 排除。

## 常用操作

```bash
# 数据卷：必须为卷分配稳定 ID
dbas volume register <挂载点> --id era5hd
dbas volume list

# 扫描：先盘点，再解析；budget/batch 控制单次规模
dbas scan --volume era5hd --mode inventory
dbas scan --volume era5hd --mode inventory --enqueue-metadata
dbas scan --volume era5hd --mode metadata --budget 60 --batch-size 20
dbas scan --volume era5hd --mode incremental

# 中断后重跑同一命令即可续跑
dbas scan --volume era5hd --mode metadata

# 校验
dbas verify --volume era5hd
dbas checksum --volume era5hd --batch-size 5

# 任务、报告与数据库维护
dbas tasks list --status pending
dbas tasks show TASK_ID
dbas tasks prune
dbas report inventory
dbas db check
dbas db backup

# 数据集、画像与 QC
dbas dataset list
dbas dataset assign --volume era5hd
dbas profile --volume era5hd --limit 100
dbas qc run --volume era5hd
dbas qc report --volume era5hd
dbas qc report --dataset era5_pressure

# 区域裁切（需要 .[eda]；源卷只读）
dbas subset plan --volume era5hd
dbas subset enqueue --volume era5hd --region qhp_main --variables t2m,tp
dbas subset run --budget 600 --batch-size 5
```

任务通常写入 `data/catalog.db`，报告写入 `data/reports/`，subset 产物写入 `data/analysis/`。这些路径均属于运行时产物，不应提交到 Git。

## CDS API 下载

1. 在 [Copernicus Climate Data Store](https://cds.climate.copernicus.eu/) 注册并获取 API Key。
2. 使用环境变量 `CDSAPI_URL`/`CDSAPI_KEY`，或写入用户目录下的 `~/.cdsapirc`；凭证不要提交。
3. 先预览请求，再入队并执行：

```bash
.venv/bin/python -m pip install -e '.[fetch]'
dbas fetch plan --config configs/fetch/era5_demo.yaml
dbas fetch enqueue --config configs/fetch/era5_demo.yaml
dbas fetch run --config configs/fetch/era5_demo.yaml
```

下载支持 `.part` 临时文件和原子改名；成功后文件会自动登记到 catalog 并入队元数据解析。请始终先执行 `fetch plan`，确认时间范围、变量、区域和文件大小。

## 配置与安全边界

- 默认配置在 `src/dbas/core/config.py`，项目级覆盖在 `configs/system.yaml`；批次、预算、资源水位和重试策略均可配置。
- 机器专属路径放在未提交的 `configs/local*.yaml` 或环境变量中；不要把凭证、数据库、下载文件和真实数据路径写入公共配置。
- 推荐使用小批次、低并发和断点续跑。资源水位是“尽量早停”的软保护，不是 OS 层绝对内存隔离。
- 真实数据盘的挂载、离线卷处理和备份策略见 [docs/operations.md](docs/operations.md)；系统设计见 [docs/architecture.md](docs/architecture.md)。

## 项目结构

```text
src/dbas/
├── storage/         卷身份识别与挂载辅助
├── scanner/         inventory / incremental / metadata / verify / checksum
├── scheduler/       原子认领、预算、重试与陈旧任务回收
├── resource_guard/  RSS/可用内存软保护
├── fetch/           ERA5 CDS API 下载器
├── reports/         Markdown 报告生成
└── cli.py           统一命令入口
scripts/             合成数据与实验性分析脚本
tests/               合成数据测试（默认不联网、不依赖真实 ERA5 卷）
```

核心算法（配置/异常/日志、catalog、数据集识别、画像、区域裁切、QC、NetCDF 头解析）
已拆分至独立仓库 [dbas-core](https://github.com/Neiyako/dbas-core)（包名 `dbas_core`），
本仓库安装时自动以 Git 依赖方式拉取。

## 测试

测试使用临时目录和合成 NetCDF，覆盖 catalog、CLI、扫描器、任务调度、fetch 管理、数据集识别、画像/QC、subset 与云水代理计算。完整运行：

```bash
.venv/bin/python -m pip install -e '.[dev,eda]'
.venv/bin/python -m pytest
```

只运行某一组：

```bash
.venv/bin/python -m pytest tests/test_catalog.py tests/test_cli.py
.venv/bin/python -m pytest tests/test_subset.py tests/test_profile.py
```

## 路线图与限制

- [x] 阶段 1：配置、CLI、SQLite、卷注册、分批扫描、预算、断点续跑
- [x] fetch：CDS API 小体量下载与 catalog 集成
- [x] 阶段 4a：区域裁切（qhp_main/qhp_wide、xarray+dask、任务队列）
- [x] 阶段 4b 首版：内容画像与 QC 证据
- [ ] 阶段 4b 完整：更多 QC 规则、累计量/瞬时量判别和人工复核
- [ ] 阶段 4c：通用气候态、季节循环、海拔分层统计与图表
- [ ] 阶段 5：非破坏性清洗、特征工程、Parquet 导出与建模接口
- [ ] 阶段 3 强化：危险解析的子进程隔离与父进程超时监控

已知限制包括：非 ISO 日历（如 `360_day`）当前只记录未知并告警；画像为抽样统计而非逐格全量统计；文件级覆盖率依赖规范化文件名；资源保护不能替代操作系统级隔离。
