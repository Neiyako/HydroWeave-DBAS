# 运行手册与数据安全

## 首次使用安全检查清单

- [ ] 确认原始数据盘为只读习惯使用：DBAS 自身从不写原始卷，但不要把
      `data/` 目录建在原始数据盘上。
- [ ] `./bin/dbas status` 确认指向预期项目根。
- [ ] 注册卷时用 `--id` 显式指定稳定 ID（跨重挂载不变）。
- [ ] 第一次扫描先 `--mode inventory`（只盘点，不解析），确认文件数符合预期。
- [ ] 用 `--budget 60 --batch-size 20` 小预算试跑元数据解析，观察输出。

## 硬盘离线怎么办

- 扫描时卷不可访问 → 报错退出（exit code 3），**不会**把文件标记为已删除。
- 文件只有在「卷在线 + 完整扫描成功」后仍未发现，才被标记
  `missing_pending`（待确认缺失），等你人工确认。
- 卷被显式标记 offline 后其文件显示 `offline` 状态；重新挂载后
  `volume register` 同一 ID 即恢复 online，文件状态在下次扫描自动恢复。

## 断点续跑与失败处理

- budget/batch/资源水位触发的中断都会打印恢复提示；**重跑同一命令即可**。
- 查看失败任务：`tasks list --status failed`，诊断信息在 `tasks show ID`。
- 修复问题后重跑：failed 任务不会被自动重试；如需重试，删除该任务
  （`sqlite3 data/catalog.db "DELETE FROM tasks WHERE task_id='...'"`）后
  重跑扫描命令即可重新入队（dedup_key 含 mtime，文件没变也会重新入队的是
  新任务；详见 tests/test_scanner.py::test_incremental_only_enqueues_changed）。
- 进程被强杀：`running` 任务的心跳停止，超过
  `scheduler.stale_running_s`（默认 600 秒）后，下次启动自动回收为 `pending`。
  阈值不要设为 0，否则会误回收仍在工作的并发任务。

## 资源保护的真实边界

- `resource_guard` 监控进程 RSS 与系统可用内存，达到警戒线后**不再认领新任务**，
  当前正在处理的文件会正常完成并提交。
- 这不是绝对安全保证：OS 的内存管理、swap、磁盘 I/O 调度不受本程序控制；
  RSS 检查间隔为「每文件一次」，极端巨大的单文件元数据仍可能瞬时超线。
- 因此真正的保护是组合拳：单进程默认并发 + 小批次 + 时间预算 + 每文件小事务
  提交 + 断点续跑。请勿把 budget 调到数小时不中断地跑大盘。
- BLAS/数值库线程通过环境变量限制为 1（`core/config.py` 顶层设置）。

## 数据库维护

- 备份：`./bin/dbas db backup`（SQLite 在线备份 API，可在使用中执行）。
  备份先写同目录 `.part`，对副本跑 `integrity_check` + `foreign_key_check`，
  通过后才原子替换目标文件；失败会删除临时文件、保留旧备份，不会留下半截文件。
- 检查：`./bin/dbas db check`（integrity_check + foreign_key_check）。
- 恢复：停掉正在运行的 `dbas` 进程 → 把 `data/backups/catalog_<时间戳>.db`
  拷回 `data/catalog.db`（连同删除同目录 `-wal`/`-shm`）→ `dbas status` 确认。
- 重建：`db backup` → 移走 `data/catalog.db` → 重新注册卷并扫描。
  原始数据不受影响；唯一损失是任务历史，文件索引由原始文件重建。

## 内容画像与 QC

- 先小批量试跑：`./bin/dbas profile --volume <卷> --limit 20`。
  画像会抽样读首/末时次，不读完整文件；budget/batch 与扫描一致，可断点续跑。
- 全量画像：`./bin/dbas profile --volume <卷> --batch-size 500 --budget 900`，
  重复执行直到 `tasks list --status pending` 为空；已完成文件的 dedup 会自动跳过。
- `dataset list` 查看数据集分组与画像进度；`dataset assign --volume <卷>`
  只做分组，不读数据。
- `qc report --volume <卷>` 只从 catalog 生成 Markdown，不重读原始文件。
- 现有规则覆盖：文件截断/尾部未写入、时间轴非单调、fill/缺测比例、
  物理范围、负比湿、未声明的 -999 缺测哨兵、常数场、轴缺失、数据集覆盖率。
- 画像统计是抽样结果，不能替代单格/全量精确分析；规则命中会写入
  `quality_reports`，带证据 JSON 与 `needs_human` 标记，原始数据永远不被修改。

## 下游实验与绘图（不在本仓）

本仓到「资产可定位、可校验」为止。科研实验、物理量计算与出图在独立的
Hydroweave 实验仓里，它只读本仓 catalog：

```bash
# 1. 先在 DBAS 侧把卷和数据集索引好（本仓）
./bin/dbas volume register /Volumes/Untitled --id era5hd
./bin/dbas scan --volume era5hd --mode inventory --enqueue-metadata
./bin/dbas scan --volume era5hd --mode metadata
./bin/dbas dataset assign --volume era5hd

# 2. 再到实验仓按 run-id 计算与制图（Hydroweave 仓）
#    cd ~/Desktop/Hydroweave && PYTHONPATH=src:. .venv/bin/python \
#      experiments/tp_cloud_water/eda.py --config experiments/tp_cloud_water/config.yaml --run-id tp-2010-2013
```

约定：

- 实验仓**只读** catalog，不写回、不改原始卷；本仓不感知实验产物；
- 需要比特级归档证据时，先在本仓 `./bin/dbas checksum --volume era5hd` 分批生成校验和；
- 实验产物、运行清单与验收报告由实验仓自己管理，不进本仓 `data/`。

## 已声明的未实现项

清洗、特征工程命令尚未提供；QC 目前是首版规则集，人工复核流程和更多物理
一致性规则（如探空/再分析交叉检验）尚未实现。科研实验与绘图在 Hydroweave
实验仓，本仓只承担数据资产层。
CDS 服务器排队时间不可控，fetch 不设硬超时，中断后重跑即可。

## 数据盘：`era5hd` = /Volumes/Untitled

1TB USB 移动盘，单分区 NTFS（Apple FSKit 驱动挂载为**只读**）。
容量 785GB 已用 / 215GB 空闲；59,503 个文件已全量入册。

| 盘上位置 | 内容 | 文件数 | 体量 | catalog 处理 |
|---|---|---|---|---|
| `2010/`–`2013/` | ERA5 0.25° 逐日单变量 NetCDF（每年 5–10 月 × t/q/u/v，命名 `ERA5_025dg_YYYYMMDD_<var>.nc`） | 2,944 | 689GB | 已解析元数据；dataset=`era5_pressure` |
| `DATA/` | 专题数据集混装：MODIS 大气成分、ERA-Interim、冰川/湖泊/积雪数据集、WPS-WRF 输出、shp 矢量、中文命名的下载包 | 56,559 | 37.7GB | 全量清单化，标记 `not_netcdf`（可检索、不解析头） |

约定与注意：

- **盘保持只读**：DBAS 不写原始卷，catalog 与产物都留在 Mac 侧 `data/`。
  SQLite 不要放到 NTFS/FUSE 卷上——跨平台文件锁不可靠，掉电易损坏索引。
- **索引不随盘走**：换机器/重装后需 `volume register /Volumes/Untitled --id era5hd`
  （ID 不变则 rel_path 键全部复用）+ 重跑 inventory；`data/backups/` 里的
  `catalog_*.db` 是索引备份，恢复用 `cp` 覆盖 `data/catalog.db` 即可。
- **启动用 `./bin/dbas`**，不要用 `.venv/bin/python -m dbas`：editable 安装依赖
  site-packages 里的 `.pth`，而 iCloud/同步代理会把 `.pth` 打上 macOS `hidden`
  标志位，Python 3.13 的 `site.py` 直接跳过它 → `No module named dbas`。启动器
  改用 `PYTHONPATH=src`，与该标志位无关。若想修复 console script，可执行
  `chflags nohidden .venv/lib/python3.13/site-packages/*.pth` 后重试
  `.venv/bin/dbas --help`（本项目 2026-11-03 就是这样恢复的）。
- **全量清单化**开关在 `configs/system.yaml`：`scan.inventory_extensions: ["*"]`
  收录所有文件；`scan.nc_extensions` 仍只决定哪些进元数据解析队列。
  `scan.exclude_dirs` 剪掉 `$RECYCLE.BIN`、`System Volume Information` 等卷级噪声
  （盘在 Windows 上用过，这两个目录必然存在）。
- 日常增量：新拷文件后跑 `./bin/dbas scan --volume era5hd --mode incremental`，
  它只把新增/变化的文件入队解析。
- 报告：`./bin/dbas report inventory`。
- **尚未做**：689GB ERA5 的 sha256 基线（`checksum` 只跑过 5 个样本）。只读档案
  的唯一比特级保障就是校验和，建议低峰期分批跑 `--batch-size 5 --budget 600`
  反复执行；在这之前，盘上数据出现静默损坏是无法察觉的。
- 数据集自动识别（`dataset assign`）当前只覆盖 netCDF，`DATA/` 里 4 万余个
  `.gz`/`.tif` 等条目 `dataset_id` 仍为空，`dataset list` 因此低报盘的构成。

## CDS 抓包（应用层 + 代理层）

三层证据分开判定，缺层就写 `INSUFFICIENT_EVIDENCE`，不用“没抓到”推断“服务没返回”：

| 层 | 触发方式 | 产物 |
|---|---|---|
| 应用层 | `./bin/dbas fetch run --config ... --capture-dir DIR` | `DIR/events.jsonl`（application_request/response/error） |
| 代理层 | `scripts/cds_capture_proxy.py` + `HTTPS_PROXY` | `DIR/events.jsonl`（request/response，含状态码与路径）+ `proxy_manifest.json` |
| 连接层 | `scripts/cds_packet_capture.sh`（tcpdump，只记字节不解析 TLS） | `pcap_manifest.json` |

离线出报告：`./bin/dbas fetch report --capture-dir DIR --output data/reports`。

**2026-11-03 实测要点（都踩过，写下来避免重复）**：

- 代理必须指向**当前** CDS 主机 `cds.climate.copernicus.eu`；旧值
  `api.ecmwf.int` 是上一代 REST 端点，命中不了任何流量。
- 让 `cdsapi` 经 mitmproxy 走全链路，需要三件事同时成立：`HTTPS_PROXY`
  指向本地代理、`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` 指向「certifi + mitm CA」
  的合并证书、`no_proxy` 不含该主机。`cdsapi.Client(verify=...)` 单个参数不够，
  它内部的 `ecmwf.datastores` 客户端会自建 session，仍需环境变量兜底。
- 代理用 `kill`/`pkill` 停止时是 SIGTERM，`asyncio.run` 不会走 `finally`；
  脚本已加信号处理并**每写一条事件就刷新** `proxy_manifest.json` 的 `stats`，
  否则报告拿不到“代理是否真的抓到流量”。
- 单请求规模过大时 CDS 直接返回 `403 cost limits exceeded`（实测 31 天 ×
  24 时次 × 37 层，21/21 全失败，耗时仅 0.5–3 s）。全量配置已改用
  `split_by: day` 逐日拆分（744 个请求），单请求降到 1/31。
- 逐日拆分后的请求是**慢**而不是卡死：文件会持续增长（实测 1→2→3→4→6 MB），
  过程中 `%CPU` 可能为 0、`lsof` 也不一定有活跃 TCP。判断进度看**文件大小变化**，
  不要因为短时无增长就杀进程——那样只会留下打不开的半截 nc。
