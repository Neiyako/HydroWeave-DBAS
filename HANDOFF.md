# 交接单 · 2026-11-03 傍晚进度

> 本文件在 2026-11-03 傍晚重写。此前的版本描述 `experiments/tp_cloud_water`
> 实验层与 62 passed，那部分已在 commit `5a7e348`「重构定位：剥离实验与绘图层」
> 中**整体移出本仓**（迁往 HydroWeave 实验仓）。旧内容不再是本仓状态，勿据此操作。

## 一、当前定位

DBAS 只做**科研数据资产管理**：清单化、指纹、QC、数据卷、任务编排与派生。
不含实验代码、物理量计算与绘图——那些在 HydroWeave 实验仓。

能力链：`scan → catalog → profile/qc → subset(L1 .nc) → zarr_export(L2 数组层) → fetch`。

## 二、本轮完成：L2 数组层（Zarr 派生）

| 改动 | 证据 |
|---|---|
| 新增 `src/dbas/zarr_export/`（store/tasks/manifest/config_ext，约 680 行） | 分块由查询模式反推：`field_series` / `point_series` / `area_stats` / `level_profile` |
| 新增 `tests/test_zarr_export.py`（27 用例） | 分块、往返一致性、清单哈希、catalog 登记、clobber 保护 |
| `dbas zarr` 子命令：`plan/enqueue/run/verify/info` | 复用 Runner 的预算/批次/重试/断点续跑语义 |
| 清单哈希补齐 Zarr 比特级证据缺口 | `sha256(排序后 "路径\tsize\tmtime_ns\tsha256" 拼接)` 写入 `files.fingerprint`（`manifest-sha256`） |
| 同步 README / docs/architecture / docs/zarr-layer.md / configs/system.yaml | 新增 `zarr` 配置段与 extras |
| `configs/constraints-py313.txt` 记录实测版本 | zarr 3.4.2、numcodecs 0.17.1 等 |

## 三、验证证据（本轮实测）

- 全量测试：`.venv/bin/python -m pytest` → **90 passed**（含 zarr 27 项）。
- `./bin/dbas zarr plan --source <合成 4D nc>` → 四种布局均正确反推分块，exit 0。
- 四种布局真实写入 + `xr.open_zarr` 回读：**逐元素完全相同**（`roundtrip_equal=True`），
  store 文件数 11–19，清单 digest 正常生成。
- 可用性约束：「`python -m dbas` 在本机不可用」是**既有已知限制**（.pth 被 macOS 打
  `hidden` 标志），日常必须用 `./bin/dbas`；见 docs/operations.md。`zarr-layer.md`
  命令示例为通用写法，等效操作请用 `./bin/dbas zarr ...`。

## 四、分支与推送状态（重要）

`origin` = `Neiyako/HydroWeave.git`，**一个远端承载两个仓的分支**：

- `origin/main` = **HydroWeave 实验仓**（`f574eb3` / `26beb91`），与本仓历史**无关联**；
- `origin/feat/fetch-hardening` = **本仓 DBAS 的远端对应分支**，与本地 `main` 一致；
- 因此 DBAS 的改动**不得推送 `main`**（会污染实验仓，或需无关历史合并）。

本轮 L2 数组层提交到本地 `main`，并推送到 `origin/feat/fetch-hardening`。

## 五、未完成 / 下一步

1. 跨文件合并为单一时间轴 store（现为一源文件一 store）——见 `docs/zarr-layer.md` 第五节。
2. 真实 ERA5 卷上的 `zarr enqueue/run` 尚未跑过，只有合成数据证据。
3. `dbas-core` 升级流程演练（当前只钉 commit，无自动化升级检查）。
4. 真实卷全量 `checksum` 基线未跑，需比特级归档证据时按 docs/operations.md 低峰分批。
5. `.research/`（IFS 手册 PDF 约 20MB）已加入 `.gitignore`，保持未跟踪、不删。

## 六、恢复方式

`git status` 对照上表定位。L2 数组层相关产物的边界：原始卷只读，`data/arrays/`
下的 store 可随时删掉重建，不影响任何既有能力。
