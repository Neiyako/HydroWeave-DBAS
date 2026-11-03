# 架构说明

## 设计原则

1. **原始数据只读**：所有写操作只发生在 `data/`（数据库、报告、下载、备份）。
2. **最小粒度任务**：每文件一任务，天然支持断点续跑、单点失败隔离、重试。
3. **流式处理**：目录遍历用 `os.scandir` 生成器；校验和按 1MB 分块；NetCDF
   只读 header。任何路径都不把 2TB 数据整体载入内存。
4. **显式未知**：解析不了的元数据记 NULL 并生成警告，绝不推断填充。
5. **状态语义分明**：文件有 `new/active/missing_pending/offline/removed`；
   解析有 `unparsed/parsed/failed/not_netcdf`；任务有
   `pending/running/succeeded/failed/retry_wait/cancelled/blocked`。

## 模块依赖方向

```
cli → scanner / fetch / reports / scheduler
scheduler(runner) → catalog（任务表）、resource_guard
scanner → catalog、inspection
fetch → catalog（下载注册 + 入队 scan_metadata）
catalog → core（配置、异常）
cli → storage/backup（db backup 的在线备份与校验）
experiments/* → repro/manifest、catalog（只读查询）
```

`fetch` 和 `scanner` 平级，二者都经由 catalog 写入同一索引——这就是
「小体量下载测试管理链路」的实现方式：下载文件注册成 `downloads` 卷中的
普通文件，之后的 verify/checksum/metadata 流程与外接硬盘完全一致。

## 数据库（v1）关键关系

- `volumes 1—N files`（volume_id），文件主键 = sha1(volume_id + rel_path)。
- `datasets 1—N files`（dataset_id）：数据集跨卷、卷可混放多个数据集。
- `files 1—N variables / dimensions`（file_id，解析成功后整体替换）。
- `files 1—1 file_profiles`；`file_profiles 1—N variable_profiles`（画像结果）。
- `quality_reports` 记录文件/变量/数据集级发现与证据（text JSON）。
- `tasks 1—N task_attempts`（每次认领一条尝试记录）。
- `scan_runs` 记录每次 CLI 扫描运行的模式与统计。
- `schema_migrations` 驱动版本化迁移（新增版本在 `catalog/db.py` 追加 SQL）。

## 应用层：数据集 / 内容画像 / QC

- 数据集识别只依赖卷内路径/文件名（`analysis/datasets.py`），不读内容；
  无法识别的归入 `unknown`，不猜测。
- `profile_nc` 任务抽样读取首/末时次（层全量、空间跨步），写
  `file_profiles` / `variable_profiles`，并执行文件级 QC；原始文件只读。
- 文件级规则：R001 截断（nc3 按维度/类型推算大小）、R002 末时次常数、
  R003 fill 比例、R004 物理范围、R005 负比湿、R006 时间轴、
  R007 常数场、R008 轴缺失、R009 未声明的 -999 类缺测哨兵。
- 数据集级规则：R101 轴签名不一致、R102 日期缺变量、R103 整日缺失、
  R104 重复日期+变量、R105 数据集变量整体缺失。
- 报告由 `reports/profile_qc.py` 从 catalog 生成，不重复读原始数据。

## 任务生命周期

```
enqueue(dedup_key) → pending → claim(原子 UPDATE, attempts+1) → running
  ├─ 成功 → succeeded（可被 tasks prune 清理）
  ├─ 失败 → attempts < max ? retry_wait(updated_at=now+delay) : failed
  └─ 心跳超过 stale_running_s → 下次启动 reclaim_stale_tasks → pending
```

`retry_wait` 的延迟通过 `updated_at` 时间戳实现：claim 只认领
`updated_at <= now` 的任务。时间戳为秒级精度。

陈旧任务回收阈值来自 `configs/system.yaml` 的 `scheduler.stale_running_s`
（默认 600 秒），由 CLI 传给每个 `Runner`。阈值不能设为 0：那会把仍在正常
心跳的并发任务当死任务回收，造成同一任务被两个 worker 同时执行。单机中断
后重跑同一命令即可：超过阈值的 running 任务自动回到 pending 并被续跑。

## 可复现实验层（`experiments/` + `src/dbas/repro/`）

实验代码不再放在 `scripts/` 的一次性脚本里，而是按实验分目录、配置驱动：

- `experiments/<name>/config.yaml` 声明 catalog、`volume_id`、`dataset_id`、
  年份、区域、时次、物理阈值与输出目录；机器专属路径只出现在配置里。
- `scripts/` 只留可执行入口，按用途分三类：`analysis/`（实验命令行包装，
  转发到 `experiments/`）、`demo/`（合成数据与 fixture）、`dev/`（独立验收等
  维护脚本）。约定见 `scripts/README.md`，业务逻辑一律不写进脚本。
- `src/dbas/repro/manifest.py` 提供 `config_hash`（规范化 JSON 的 SHA-256
  前 16 位）、`input_fingerprint`（catalog 的卷 ID + 相对路径 + 大小 + mtime，
  排序后哈希，不扫 2TB 内容）、`git_revision`/`package_versions` 环境快照，
  以及 `atomic_write_text/json`（同目录 `.part` + `os.replace`）。
- 运行目录 `data/analysis/<exp>/runs/<run_id>/` 内按 `yearly/ tables/
  figures/ summary/` 分层；年度结果先写 `.part` 再原子替换，成功后立刻更新
  manifest 的 `completed_years` 与产物 SHA-256。重启后跳过已完成年份。
- 配置哈希与输入指纹变化时，同 `--run-id` 直接拒绝续跑（必须换 run-id 或
  `--force`），避免不同口径的产物混在同一清单下。

## 数据库备份

`dbas db backup` 走 `src/dbas/storage/backup.py`：SQLite 在线备份 API 写同目录
`.part`，对备份执行 `PRAGMA integrity_check` 与 `PRAGMA foreign_key_check`，
通过后 `os.replace` 原子改名；任一校验失败则删除临时文件并保留旧备份。
因此 `data/backups/` 里不会出现「看起来完整但实际截断」的备份。

## fetch 设计

- 每个请求（变量集×年×月）一个任务，dedup_key = sha1(dataset+request)，
  重复 enqueue 自动跳过。
- 下载 → `<target>.nc.part` → 校验非空 → `os.replace` 原子改名。
- 成功后：`upsert_file` 注册进 downloads 卷 + 入队 `scan_metadata`。

## 测试策略

全部使用合成 NetCDF（`tests/conftest.py: make_nc`）与合成实验卷
（`scripts/demo/make_tp_cloud_water_fixture.py`），不依赖真实数据、外接盘或
网络；fetch 用注入的假客户端（`CLIENT_FACTORY`）。覆盖：幂等、预算中断恢复、
损坏文件隔离、卷离线不误删、原子认领、陈旧任务回收与 `stale_running_s` 语义、
资源判断纯函数、CLI 全流程、manifest 哈希/指纹/原子写、在线备份校验与失败清理、
云水代理实验的 EDA→manifest→断点续跑→制图全链路。
