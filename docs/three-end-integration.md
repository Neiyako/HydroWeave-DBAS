# 三端对接：ResearchDataHub → DBAS → Hydroweave

本文件说明三个仓库如何拼成一条可复核的数据—证据链，以及每一端的接口契约、失败边界与复现命令。
判定口径：每一步都要有**可核对的证据**（SHA-256、catalog 行、图件、五态结论），退出码 0 不算证据。

---

## 1. 分工与数据流

```
ResearchDataHub（采集端）              DBAS（资产端）                     Hydroweave（科学端）
需求 YAML                               catalog.db（SQLite 事实来源）      budget.py / 云水模型
  │ plan create（联网查目录）             ▲                                ▲
  │ task run（下载 + 完整性校验）          │ register/upsert/解析             │ 按 volume_id + dataset_id
  ▼                                       │                                │ 读 catalog，不写死路径
raw/<file>.nc + deliveries/<id>.json ──► dbas fetch rdh ──► scan metadata ──┘
（逐文件 sha256、size_bytes、complete）   （复核 sha256 → 原子拷贝 → 登记 → 入队解析）
```

三端都**不共享 Python 包**：接缝是「交付清单 JSON + 文件系统」与「DBAS catalog（SQLite）」，
两端各自可独立升级。宁可多一份 45 KB 的副本，也不让科学端直接依赖采集端的内部状态。

| 端 | 负责 | 明确不做 |
|---|---|---|
| ResearchDataHub | 发现数据、提交 CDS 请求、下载、完整性校验、生成交付清单 | 不做科学计算、不解析变量语义 |
| DBAS | 登记资产、解析 NetCDF 头、维护 catalog、提供查询与数组层 | 不做误差分析、不产生科学结论 |
| Hydroweave | 物理量积分（LWP/IWP）、收支模型（GCWR）、误差与图件 | 不决定“数据从哪来”，只消费 catalog |

---

## 2. 接口契约

### 2.1 ResearchDataHub → DBAS：交付清单

位置：`<storage.root>/deliveries/<delivery_id>.json`（默认 `~/.rdh/deliveries/`）。
DBAS 只信任这份清单与它引用的文件字节，不扫描 RDH 数据库内部表。

| 字段 | 含义 | DBAS 的用法 |
|---|---|---|
| `delivery_id` | 交付标识 | 记入抓包事件与 e2e 报告 |
| `request_id` | 需求标识（与请求 YAML 对齐） | 定位「本次该导入哪份交付」 |
| `complete` | 是否全部文件通过完整性校验 | 必须为 `true`，否则拒绝导入 |
| `files[].path` | 相对 `storage.root` 的路径 | 拼绝对路径，校验不得逃逸 |
| `files[].sha256` | RDH 计算的校验和 | **导入前后各算一次**，不等即失败 |
| `files[].size_bytes` | 字节数 | 与落盘大小核对 |
| `excluded[]` | 失败/跳过/隔离的文件 | 保留在报告里，不导出为交付 |

### 2.2 DBAS → Hydroweave：catalog

Hydroweave 只读 DBAS catalog（`data/catalog.db`），按 `volume_id + dataset_id` 找文件：

| 表 | 关键列 | 科学端关心的内容 |
|---|---|---|
| `volumes` | `volume_id`, `last_mount_path` | 卷在线时文件根的绝对路径 |
| `files` | `file_id`, `rel_path`, `size_bytes`, `mtime`, `dataset_id`, `status` | 哪些文件属于本数据集、是否 active |
| `file_profiles` | `n_vars`, `time_n`, `lat/lon/level` 范围 | 网格与时间覆盖（解析后写入） |
| `variables` | `name`, `units`, `dims` | `clwc`/`ciwc` 是否真的存在、单位是什么 |

`scan --mode metadata` 是让 `file_profiles`/`variables` 从“空”变成“可查”的那一步。没有这一步，
Hydroweave 只能看到文件名，看不到变量——这是 e2e 里最容易被漏掉的一环。

### 2.3 校验链（三代 SHA-256 必须一致）

```
RDH deliveries/<id>.json: files[].sha256
        │  （dbas fetch rdh 复核）
        ▼
DBAS data/downloads/<dataset>/<file>.nc   ← 落盘副本（RDH raw 原文件保持 0444 只读，不动）
        │  （Hydroweave inputs.json 记录）
        ▼
Hydroweave runs/<run-id>/inputs.json: files[].sha256
```

e2e 脚本 `scripts/e2e/three_end_e2e.py` 在报告里逐项打印这三个值，任一不等即判 FAIL。

---

## 3. 代码落点

| 仓库 | 文件 | 作用 |
|---|---|---|
| ResearchDataHub | `src/researchdatahub/transfer/cds_api.py` | `CdsApiTransport`：下载 CDS 结果签名 URL（路由判别 + 标识，流式/续传/重试继承 HTTP 实现） |
| ResearchDataHub | `src/researchdatahub/cli/_services.py` | `build_transports()` 装配 `cds_api` 传输，`task run` 不再报 “no transport adapter” |
| DBAS | `src/dbas/fetch/rdh.py` | 子进程驱动 RDH `plan create → task run → report generate`，复核交付清单并导入 catalog |
| DBAS | `src/dbas/cli.py` | `dbas fetch rdh --config ... [--capture-dir] [--plan-id] [--dry-run]` |
| DBAS | `configs/fetch/rdh_sample.yaml` | 小样本采集需求（clwc/ciwc，1 时次，500/400/300 hPa，青藏高原） |
| DBAS | `scripts/e2e/three_end_e2e.py` | 三端编排 + 校验链 + 报告（`data/reports/e2e/<run-id>/`） |
| Hydroweave | `experiments/cloud_water_strict/` | 读 catalog → LWP/IWP → 恒等式自检 → 图件 / 五态报告 |

---

## 4. 复现

```bash
# 0) 前置：CDS 令牌（~/.cdsapirc 或 CDSAPI_KEY）；本机另需两个仓的解释器
#    ResearchDataHub/.venv 与 Hydroweave/.venv

# 1) 只跑 RDH 采集 + DBAS 导入（含脱敏事件流）
cd ~/Desktop/era5数据dbas
./bin/dbas fetch rdh --config configs/fetch/rdh_sample.yaml

# 2) 让 DBAS 解析元数据（变量 clwc/ciwc 进入 catalog 才能被科学端查询）
./bin/dbas scan --volume downloads --mode metadata

# 3) 三端一键 e2e（含 sha256 校验链与报告）
./bin/python scripts/e2e/three_end_e2e.py --run-id e2e-$(date +%Y%m%d-%H%M)
```

失败时看哪里：

| 现象 | 先查 |
|---|---|
| `plan create` 长时间不返回 | CDS 服务端排队（RDH 在发现阶段 submit→poll，最长 `cds_api.request_timeout_seconds=3600`）；用 `rdh task status` / job 状态确认，不要并发重试 |
| `no transport adapter supports access_route='cds_api'` | `build_transports()` 未装配 `CdsApiTransport` |
| 交付清单 `complete=false` | RDH 完整性校验失败，文件可能进了 `quarantine/`；DBAS 会拒绝导入 |
| Hydroweave 找不到变量 | 忘了第 2 步的 `scan --mode metadata`；或 dataset_id 不是 `era5_cloud_water` |

---

## 5. 实测记录（2026-11-04，run-id `real-catalog-20240101`）

| 步骤 | 结果 | 证据 |
|---|---|---|
| RDH 计划/传输/完整性/交付 | PASS | 计划 `plan-36f331c13fd1237f`；交付 `delivery-e448aa88fd0d4f35.json`；`raw/era5-pressure-levels/canary_clwc_ciwc.nc` 0444；完整性 `passed`，45855 B |
| DBAS 导入 + 元数据解析 | PASS | `data/downloads/era5_cloud_water/canary_clwc_ciwc.nc`；catalog 变量表含 `clwc`/`ciwc`；SHA-256 与交付清单一致 |
| Hydroweave 计算 + 图件 | PASS | run `data/analysis/cloud_water_strict/runs/real-catalog-20240101/`，5 张 PNG，`input.sha256` 链一致；整体五态 `INSUFFICIENT_EVIDENCE`（单时次 + 部分列；恒等式/权重/单位/部分列口径 PASS） |
| 三端 e2e 报告 | PASS | [`docs/evidence/three-end-e2e-20261104.md`](evidence/three-end-e2e-20261104.md)（运行时产物在 `data/reports/e2e/real-catalog-20240101/`，data/ 不入 Git） |
| 抓包事件 | 已记录 | 运行时：`data/captures/rdh_clwc_ciwc/events.jsonl`（rdh_plan / rdh_task / rdh_delivery / import，脱敏） |

**必须如实说明的边界**：本次 RDH 交付走的是**重放通道**——CDS 服务端生成阶段在
2026-11-04 晚间被保护性限流阻塞（压力层 job 提交后 2 小时仍 `accepted`，新提交被队列上限
`rejected`），因此没有重新提交 CDS 请求；数据本体是 2026-11-03 已通过 CDS API 真实下载的
同一份 ERA5 clwc/ciwc 产物，RDH 的传输（HTTP 流式 + `.part` 原子提交）、校验（size + SHA-256
+ NetCDF 可打开）、交付清单与 DBAS 导入都是真实执行。CDS 队列恢复后，去掉 `--plan-id`
用 `configs/fetch/rdh_sample.yaml` 重跑同一套命令即可得到"新提交 + CDS 传输"的完整版本。

## 6. 已知边界（不是 bug，是契约）

1. **CDS 排队不可控**：`plan create` 在发现阶段就会 submit→poll→results，最坏等满
   `request_timeout_seconds`；队列拥塞时 45 分钟以上未启动是实测现象。恢复方式是稍后重跑
   （同一请求命中 CDS 缓存后通常 <1 分钟）。不要并行双投——违反 CDS 串行政策。
2. **RDH 的 CDS 时间展开是笛卡尔积近似**：请求 `day` 取覆盖月份的天数并集
   （1 天窗口 → 31 天），这是 RDH 已文档化的行为，样本仍是“小样本”但字节数会放大 ~31×
   （本样本约 6 MB）。要精确按天取需要修改 `_covered_days` 并同步调整测试，本次未改。
3. **签名 URL 会过期**：`CdsApiTransport` 对 401/403 判为不可重试失败；恢复 = 重新
   `plan create` 拿新 URL，不做自动 re-submit。
4. **Hydroweave 的部分列不当整列**：样本只有 300/400/500 hPa（占名义整列 20%），
   报告显式写 `partial_column=true` 且不外推；LWP/IWP 是“部分柱含量”，不是整列。
5. **GCWR 收支闭合本轮 NOT_RUN**：缺降水/蒸发/边界通量与更长时段样本，恒等式只做了
   代数自检（机器精度残差），不构成科学结论；判定见 `runs/<run-id>/summary.json`。
