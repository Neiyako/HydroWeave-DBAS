# 交接单 · 2026-11-03 中午进度

目标：项目结构清晰 + 实验高可用（单机容错/可续跑）+ 可复现。

当前判定：**PASS**。合成全链路通过；真实卷 2010–2013 全量 EDA 已完成
（736 天 / 24 月 / 0 失败），新 run 与旧扁平产物逐年 **max abs diff = 0.0**；
真实卷独立验收 `scripts/dev/validate_real_eda.py` 六项硬标准全 PASS
（catalog/盘一致、季节覆盖、抽样物理区间、单柱独立重算 0.0000% 误差、
新旧回归、产物清单）。未做 push。

## 已完成（工作区，尚未提交）

| 改动 | 证据 |
|---|---|
| `experiments/tp_cloud_water/plot.py` 完成 run-dir 重构 | 读 `yearly/` + `tables/`，写 `figures/` + `summary/`，产物指纹登记回 manifest；缺月份不再画空面板；basemap 离线自动跳过 |
| 旧 `scripts/analysis/tp_cloud_water_{eda,plot}.py` 改为薄包装 | `env -u PYTHONPATH .venv/bin/python scripts/tp_cloud_water_eda.py --help` 可用 |
| 新增 `scripts/demo/make_tp_cloud_water_fixture.py` | 合成 mini catalog + 37 层 q/t（地形以下等值填充）+ mask + config，不依赖外接盘 |
| `tests/test_tp_cloud_water_experiment.py` | fixture→EDA→manifest→resume→plot 全链路 + 配置不一致拒绝 + 缺 mask 明确失败 |
| `tests/test_repro_manifest.py`、`tests/test_backup.py`、`tests/test_scheduler.py` 追加 | manifest 哈希/指纹/原子写、备份校验失败清理、`stale_running_s` 不误回收 |
| `pyproject.toml`：dbas-core 钉 `46b32f45de90`、Python 3.11+、eda extras 加 pyshp | 与已安装 dbas-core commit 一致；`configs/constraints-py313.txt` 记录实测版本 |
| README / docs/architecture / docs/operations / experiments README 同步 | 新目录、run 结构、续跑与备份语义、.pth hidden 修复说明 |
| 环境修复：`.venv/.../*.pth` 被 macOS 打上 `hidden`，Python 3.13 `site.py` 跳过 → `import dbas` 失败 | `chflags nohidden .../*.pth` 后 console script 可用；iCloud 会再次打回，日常用 `./bin/dbas` 或 `PYTHONPATH=src` |
| 真实数据测试：`tests/test_tp_cloud_water_real.py`（9 个 `-m real` 用例） | 全 PASS；覆盖 catalog/盘一致、736 天覆盖、单日物理区间、单柱 PW 独立重算、真实 run manifest/回归/图件 |
| 真实卷独立验收：`scripts/dev/validate_real_eda.py`（先定标准再执行，A–G 报告） | `VERDICT=PASS`，报告 `data/analysis/tp_cloud_water/validation/real_eda_check.md`；S4 相对误差 0.0000%，S5 16 组 max abs diff=0 |

## 验证证据

- 全量测试：`.venv/bin/python -m pytest -q` → **62 passed**（含 9 个真实卷 `-m real` 用例）。
- 合成链路：fixture 3 天 → `DONE run=fixture years=[2010] days=3 months=1 failed=0`，
  5 张图 + summary，manifest `plotted=true`。
- 真实卷 2010–2013：先跑 2010，再复用同一 run-id 续跑 2011–2013 →
  `DONE run=real-2010 years=[2010, 2011, 2012, 2013] days=736 months=24 failed=0`。
  产物在 `data/analysis/tp_cloud_water/runs/real-2010/`（4 个年度 nc、4 份逐日/
  逐月/问题表、8 张图、summary、manifest）。
- 回归对比：新 run 的 `yearly/tp_monthly_<year>.nc` 与旧扁平
  `data/analysis/tp_cloud_water/tp_monthly_<year>.nc` 逐年比较 pw/rh500/cfrac/
  surf_p，2010–2013 全部 **max abs diff = 0.0**（重构未改变科研口径）。
- 四年摘要：PW 23.2–24.4 mm；冰川区 8.8–9.4 mm；RH500 55.5–57.6%；CFRAC 4.7–5.0%；
  地下填充层 21.7%。
- 断点续跑：同 run-id 二次运行打印 `[skip] 2010`，`input_fingerprint` 不变。
- 备份：`./bin/dbas db backup` → `已备份并校验到 …/catalog_20261103_123822.db`。

## 未完成 / 下一步（优先级从高到低）

1. 旧扁平产物（`data/analysis/tp_cloud_water/*.nc|csv|png`）仍在原地：新 plot
   不读取它们。若要归档，可新增一次性迁移脚本把旧文件移入 `runs/legacy/`，勿删。
2. `dbas-core` 升级流程演练（当前只钉了 commit，未做自动化升级检查）。
3. 真实卷全量 `checksum` 基线仍未跑；需要比特级归档证据时按 docs/operations.md
   低峰期分批执行。
4. 可选：给实验层加一个 `--dry-run`/`--limit-days` 参数，便于只抽几天验证。
5. 若 iCloud 反复隐藏 `.pth`，可加一个 `scripts/dev/fix-editable-pth.sh` 或
   在 `bin/dbas` 基础上补 `.venv/bin/dbas` 包装；需先与本人确认再改全局/自动化。

## 恢复方式

`git status` 对照上表定位；真实 EDA 续跑直接重跑同命令（manifest 已完成年份跳过）。
工作区未提交改动含 `.research/`（保持未跟踪、不删）与 `HANDOFF.md`。
