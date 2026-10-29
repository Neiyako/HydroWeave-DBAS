# DBAS — ERA5 科研数据管理与分析系统

面向个人科研工作流的 ERA5 NetCDF 数据管理工具：SQLite 目录数据库、分批扫描、
任务队列、断点续跑、资源保护，以及基于 CDS API 的小体量数据下载（fetch）。

当前为 **阶段 1 + fetch** 交付：数据资产管理、扫描调度、元数据解析、下载管理
均已可用；QC / EDA / 清洗 / 特征工程为后续阶段（见「路线图」）。

## 安装

```bash
cd era5数据dbas
python3 -m venv .venv
.venv/bin/pip install -e .          # 核心依赖
.venv/bin/pip install -e '.[fetch]' # 如需 ERA5 下载（cdsapi）
.venv/bin/python -m pytest          # 跑测试（34 个，全部使用合成数据）
```

> 以下命令均可用 `.venv/bin/python -m dbas` 或安装后的 `dbas` 调用。

## 30 秒体验（不触碰任何真实数据）

```bash
.venv/bin/python scripts/make_demo_volume.py   # 生成 5 个合成 NetCDF
.venv/bin/python -m dbas volume register data/demo_volume --id demo
.venv/bin/python -m dbas scan --volume demo --mode inventory --enqueue-metadata
.venv/bin/python -m dbas scan --volume demo --mode metadata
.venv/bin/python -m dbas report inventory
.venv/bin/python -m dbas status
```

## 核心概念

- **数据卷（volume）**：一块外接硬盘/一个数据根目录。注册时分配稳定逻辑 ID，
  文件始终以「卷 ID + 卷内相对路径」定位，挂载路径变化不影响索引。
- **目录数据库（catalog）**：SQLite，位于 `data/catalog.db`，记录文件索引、
  元数据、任务队列与运行历史。可随时由原始数据重建，原始数据永远只读。
- **任务队列**：元数据解析 / verify / checksum / download 都是最小粒度任务，
  原子认领、失败重试、断点续跑；`tasks prune` 定期清理已完成任务防膨胀。

## 常用操作

```bash
# —— 数据卷 ——
python -m dbas volume register /Volumes/ERA5HD --id era5hd   # 建议显式指定 ID
python -m dbas volume list

# —— 扫描（先盘点，再解析；budget/batch 控制单次运行规模）——
python -m dbas scan --volume era5hd --mode inventory                       # 仅盘点
python -m dbas scan --volume era5hd --mode inventory --enqueue-metadata    # 盘点+入队解析
python -m dbas scan --volume era5hd --mode metadata --budget 60 --batch-size 20
python -m dbas scan --volume era5hd --mode incremental                     # 增量

# —— 中断/恢复：budget 耗尽或 Ctrl-C 后，重跑同一命令即自动续跑 ——
python -m dbas scan --volume era5hd --mode metadata

# —— 校验 ——
python -m dbas verify   --volume era5hd            # 可读性检查
python -m dbas checksum --volume era5hd --batch-size 5   # sha256（流式，1MB 缓冲）

# —— 任务队列 ——
python -m dbas tasks list [--status pending]
python -m dbas tasks show TASK_ID
python -m dbas tasks prune                       # 清理已完成任务

# —— 报告与数据库 ——
python -m dbas report inventory                  # Markdown 概览（原子写入）
python -m dbas db check                          # integrity + 外键检查
python -m dbas db backup                         # SQLite 在线备份到 data/backups/

# —— ERA5 下载（CDS API）——
python -m dbas fetch plan    --config configs/fetch/era5_demo.yaml   # 预览，不联网
python -m dbas fetch enqueue --config configs/fetch/era5_demo.yaml
python -m dbas fetch run     --config configs/fetch/era5_demo.yaml
```

### CDS API 凭证

1. 在 https://cds.climate.copernicus.eu 注册并获取 API Key；
2. `export CDSAPI_URL=... CDSAPI_KEY=...` 或写入 `~/.cdsapirc`（不入 Git）；
3. 先 `fetch plan` 核对请求范围，再 `enqueue` + `run`。
   下载以 `.part` 临时文件落盘、完成后原子改名，中断重跑不会留下半截文件；
   下载成功后自动登记进 catalog 并入队元数据解析。

## 配置

`configs/system.yaml` 覆盖内置默认值（批次、预算、资源水位、重试策略等），
所有字段可省略。机器专属路径与凭证不要提交。

## 项目结构

```text
src/dbas/
├── core/            配置、异常、日志（含 BLAS 线程数限制）
├── catalog/         SQLite schema 迁移 + 卷/文件/任务仓库
├── storage/         macOS 卷身份识别（diskutil），辅助选择稳定卷 ID
├── inspection/      NetCDF 头解析（只读元数据，未知项显式记录）
├── scanner/         inventory / incremental / metadata / verify / checksum
├── scheduler/       Runner：原子认领、预算、重试、陈旧任务回收
├── resource_guard/  RSS/可用内存水位（软保护，非绝对安全保证）
├── fetch/           ERA5 CDS API 下载器（.part 原子落盘 + 自动入 catalog）
├── reports/         Markdown 报告（原子写入）
└── cli.py           python -m dbas 统一入口
```

## 路线图

- [x] 阶段 1：配置/CLI/SQLite/卷注册/分批扫描/预算/断点续跑/测试
- [x] fetch：CDS API 小体量下载 → 自动入 catalog（管理链路测试）
- [ ] 阶段 4：QC（文件/结构/数值/时空规则）与 EDA（抽样、分块、图表）
- [ ] 阶段 5：非破坏性清洗、特征工程、Parquet 导出与建模接口
- [ ] 阶段 3 强化：危险解析的子进程隔离与父进程超时监控

## 已知限制

- 资源水位是「尽量早停」的软保护，OS 层内存管理与磁盘 I/O 无法被完全控制；
  真正的兜底是小批次 + 低并发 + 断点续跑。
- 非 ISO 日历（如 360_day）的时间坐标解码当前记为未知并生成警告，不做推断。
- 时间戳粒度为秒，重试延迟小于 1 秒无法生效。
