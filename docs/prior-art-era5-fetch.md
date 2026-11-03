# ERA5 抓取与处理的既有先例调研

调研日期：2026-11-03。目的：为 DBAS `fetch` 层找可借鉴的工程做法。
所有仓库均已实际克隆或读源码；`cdsapi` 版本以 `.venv` 中已安装的 **0.7.7** 为准。

## 一、先例清单

| 仓库 | 性质 | 核心做法 | 可信度 |
|---|---|---|---|
| [ecmwf/cdsapi](https://github.com/ecmwf/cdsapi) | 官方客户端 | `retrieve()` 可分离下载；Range 断点续传；指数退避轮询 | 读源码 |
| [bgiebl/cdsswarm](https://github.com/bgiebl/cdsswarm) | 并发下载器（与 DBAS 最接近） | 会话状态文件、请求归一化、复用服务端 job、错误分级 | 读源码 |
| [pzaninelli/downloadERA5](https://github.com/pzaninelli/downloadERA5) | 脚本封装 | 配置驱动 + CDO 后处理，几乎无容错 | 读源码 |
| [tkschuler/ERA5-Utils](https://github.com/tkschuler/ERA5-Utils) | 下载 + 校验脚本集 | `check_corruption.py`：netCDF 能打开 ≠ 数据可读 | 读源码 |
| [finnwimberly/era5_downloader](https://github.com/finnwimberly/era5_downloader) | 灵活下载/裁切 | 单文件脚本，无状态管理 | 读源码 |

整体判断：**这一领域几乎没有成熟的工程化先例**。GitHub 上 ERA5 下载器多为 1–2 个脚本、无测试、无状态管理。
唯一有工程纵深的是 `cdsswarm`（约 4600 行、有 tests），它和 DBAS 的定位重叠最多。

## 二、关键事实核对（对 DBAS 有直接影响）

1. **`cdsapi` 自身已做断点续传**。`api.py::_download` 用 `Range: bytes=%d-` 续传，
   `mode = "ab"` 追加写，`retry_max=500`、`sleep_max=120`，退避系数 1.5。
2. **`cdsapi` 已完成迁移**。`Client.__new__` 在 key 不含 `:` 时直接返回
   `ecmwf.datastores.legacy_client.LegacyClient`；本机已装 `ecmwf_datastores_client-0.5.3`，
   其 `client.py` 确有 `get_jobs()` / `get_remote(request_id)` / `get_job()`。
3. **`retrieve()` 可以不带 target**，返回 `Result`，其 `.download(target)` 可延后执行；
   `Result` 还暴露 `content_length` / `content_type` / `location`。
4. **轮询里能拿到 `request_id`**（`reply["request_id"]`），但目前 DBAS 没有记录它。

## 三、可直接借鉴的 5 条

### 1. 请求归一化后再算 dedup_key（优先级最高）

`cdsswarm::_cds_utils.normalize_request` 把所有值统一成**字符串列表**：

```python
normalized[key] = [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
```

原因是 `"year": "2024"` 与 `"year": ["2024"]` 在 CDS 侧等价，但 JSON 摘要完全不同。
DBAS 的 `request_dedup_key` 直接 `json.dumps(..., sort_keys=True)`，同一语义会产生两个 dedup_key，
导致重复下载、缓存击穿。**注意 `area` 是位置参数，list 顺序必须保留**——`cdsswarm` 保留顺序，
但 DBAS 现有 `split_by=day` 分支中 `day` 顺序会变动，需要先排序再归一化。

### 2. 记录 `cds_request_id`，实现跨进程复用

`cdsswarm::state.py` 的 `TaskState` 有 `cds_request_id`，并配 `reuse_map()` /
`clear_stale_request_ids()`。价值在于：进程被杀时服务端任务仍在排队，
重启后若重新提交会浪费队列配额；复用则直接 `get_remote(id).download(target)`。

DBAS 已有 `catalog` 和 `task_attempts`，天然适合存这个字段，
比 `cdsswarm` 的 JSON 会话文件更强（可查询、可回溯）。

**必须同时借鉴 `clear_stale_request_ids`**：会话取消后服务端任务被 dismiss，
沿用旧 ID 只会浪费重试次数。

### 3. 错误分级：可重试 vs 永久失败

DBAS 现在所有异常统一走 `retry_wait`，`max_attempts: 6`、`retry_delay_s: 60`。
但以下错误重试必然失败，白耗 6 次 × 60 秒：

- `403 cost limit exceeded`（请求超成本上限）→ 应拆请求，而非重试
- 变量/图层名拼错（`MarsNoDataError`）→ 应 fail-fast
- 未接受数据集许可（`required_terms`）→ 应提示用户去网页接受

`cdsswarm` 反其道而行：它对**所有**异常重试（`max_retries=3`，退避 `min(2**(n-1), 60)`），
但在服务端 degraded 时**无限重试**（`degraded` 分支不检查 `max_retries`）。
DBAS 更适合前一种分级：新增 `FetchPermanentError`，runner 捕获后直接 `failed`。

### 4. 产物校验不能只看"能打开"

`ERA5-Utils::check_corruption.py` 的注释点明：corrupted netCDF 常常**仍能正常打开**，
只在读数据时才出错。它的做法是打开后**实际读取**变量数组再关闭。

DBAS 目前失败路径只检查 `part.exists() and st_size > 0`，这挡不住截断文件。
建议在 `scan_metadata` 阶段补一次"抽样读数值"，把结果记进 `quality_reports`——
DBAS 本来就有 QC 证据表，属于顺手补强，不改语义。

### 5. 抓包要覆盖"提交"与"下载"两个阶段

现有 `ObservedCdsClient` 只包 `retrieve()`，于是 `application_request` 的耗时
包含了**全部排队时间**，无法区分"排队慢"和"传输慢"。
官方 `Result` 提供 `content_length`，配合 `retrieve()` 不带 target 的用法，
可以拆成两段：`submit`（拿 request_id + content_length）→ `download`（实测速率）。
这对判断 CDS 是否限流有直接价值。

## 四、明确不建议照搬的 2 条

1. **`cdsswarm` 的 JSON 会话状态文件**。DBAS 已有 SQLite catalog，
   用文件存状态会带来第二套真相来源，与"catalog 是唯一事实"的设计冲突。
2. **`configs/fetch` 里把 `area` 顺序写错就静默返回空数据** 的教训要保留防御，
   但不要学 `cdsswarm` 的"无限重试直到服务恢复"——本地单机跑批会挂着不退出，
   与 DBAS 的 budget/batch 边界检查冲突。

## 五、落地情况（已核对代码）

改造前 → 改造后：

| 能力 | 改造前 | 改造后 |
|---|---|---|
| 请求归一化 | `request_dedup_key` 直接 dumps | `normalize_request` 折叠等价写法，`area` 保序 |
| `request_id` 记录 | 无 | `download_one` 返回 `cds_request_id` |
| 错误分级 | 单一 `FetchError` | `FetchPermanentError` + runner 按 `.permanent` 跳过重试 |
| 产物完整性 | 仅大小检查 | 抓包记 `size_matches`（比对 `content_length`） |
| 抓包粒度 | 仅 `retrieve` 一层 | `submit` / `download` 两段，带实测速率 |

**尚未落地**（需真实 CDS 账号与网络，列为下一步）：

- 凭 `cds_request_id` 主动**复用**服务端已排队任务（`get_remote(id).download()`）。
  当前只做到「记录并回传」，取回逻辑未实现。
- 用 `apps.ecmwf.int/status/status` 做服务端熔断。

## 外部参照

已查 5 个仓库（含官方 `ecmwf/cdsapi`），读源码确认；另有 3 个仓库因无实质实现排除。
