# 青藏高原云水资源代理实验

这个实验只使用现有 ERA5 气压层 `t/q` 计算三个**代理指标**：

- `PW`：可降水量，水汽资源总量；
- `RH500`：500 hPa 相对湿度，成云潜势；
- `CFRAC`：100–500 hPa 内 RH≥阈值的有效层占比。

当前卷没有 `clwc/ciwc`、云水路径或总云量，因此不能把结果解释为严格云水含量。
完整物理口径（地形以下填充层的「等值连段」掩膜、Magnus 公式、梯形积分）见
[`eda.py`](eda.py) 模块注释与摘要 `summary/tp_eda_summary.md`。

## 目录与配置

```text
experiments/tp_cloud_water/
├── config.yaml   # 唯一配置入口：catalog/卷/年份/区域/时次/阈值/输出/basemap
├── common.py     # 配置加载、catalog 输入索引、run 目录与 manifest 交互
├── eda.py        # 分年计算，年度断点续跑
└── plot.py       # 读 run 目录制图 + 写 EDA 摘要
```

真实卷路径**只从 catalog 的 `volume_id` 解析**，不写死在代码里；`basemap_dir`
不在线时自动跳过底图，不影响其余图件。`config.yaml` 里的 `glacier_mask` 必须
与裁切后的 TP 网格同形。

## 运行（真实卷）

`eda.py` 的 import 同时需要 `src`（DBAS 主包）与项目根（`experiments` 包），
所以 `PYTHONPATH` 要写成 `src:.`：

```bash
PYTHONPATH=src:. .venv/bin/python experiments/tp_cloud_water/eda.py \
  --config experiments/tp_cloud_water/config.yaml --run-id tp-2010-2013

PYTHONPATH=src:. .venv/bin/python experiments/tp_cloud_water/plot.py \
  --config experiments/tp_cloud_water/config.yaml --run-id tp-2010-2013
```

旧命令仍可用（薄包装，参数相同）：

```bash
.venv/bin/python scripts/tp_cloud_water_eda.py --years 2010 2011
.venv/bin/python scripts/tp_cloud_water_plot.py
```

`--run-id` 省略时取 `run-<配置哈希>`，EDA 与制图必须用同一个 run-id。

## 合成数据自检（不接触外接盘）

```bash
.venv/bin/python scripts/demo/make_tp_cloud_water_fixture.py /tmp/tp_fixture --days 3
PYTHONPATH=src:. .venv/bin/python -m experiments.tp_cloud_water.eda \
  --config /tmp/tp_fixture/config.yaml --run-id fixture
PYTHONPATH=src:. .venv/bin/python -m experiments.tp_cloud_water.plot \
  --config /tmp/tp_fixture/config.yaml --run-id fixture
```

合成数据复刻了 37 个气压层、纬度降序、地形以下等值填充和 mini catalog；
`tests/test_tp_cloud_water_experiment.py` 用同一 fixture 验证整条链路。

## 产物布局

```text
data/analysis/tp_cloud_water/runs/<run_id>/
├── manifest.json                        # 配置哈希/输入指纹/Git/包版本/产物指纹
├── yearly/tp_monthly_<year>.nc          # 月平均场（pw/rh500/cfrac/surf_p/rh_z）
├── tables/tp_daily_<year>.csv           # 逐日面积加权记录
├── tables/tp_monthly_<year>.csv         # 逐年逐月汇总
├── tables/tp_read_problems_<year>.csv   # 读取/计算失败清单（零失败为空文件）
├── tables/tp_daily.csv ...              # 全批次汇总（EDA 结束时重建）
├── figures/                             # 图件（plot.py 写出并登记指纹）
└── summary/tp_eda_summary.md            # EDA 摘要
```

## 断点续跑与一致性

- 年度结果先写 `.part`、落盘后原子替换，manifest 记录每个产物的 SHA-256。
- 同一 `--run-id` 重跑会跳过 manifest 中已完成的年份，只补未完成年份；
  `--force` 才重算已有年度，并允许配置/输入指纹变化（会覆盖同 run 目录）。
- 配置哈希或输入指纹（卷 ID、相对路径、大小、mtime）变化时，同 run-id 直接
  报错，避免把两套配置的产物混在一个目录；换 `--run-id` 或 `--force`。
- `plot.py` 只画已完成年份，并把图件/摘要指纹登记回 manifest。
- 某年即使有个别日期读取/计算失败，只要年度产物写出仍会记入 `completed_years`，
  失败明细在 `tables/tp_read_problems_<year>.csv`（manifest 的 `failed_dates`
  汇总）。要重算这一年并重试失败日期，用 `--force`；否则续跑会跳过它。

## 复现边界

`manifest.json` 的输入指纹是 catalog 的逻辑指纹，不是全量 SHA-256；需要比特级
归档证据时，先用 `./bin/dbas checksum --volume <id>` 分批生成 catalog 校验和。
实验只读原始数据，输出写入 `data/analysis`。若外接卷不在线，真实 EDA 不能宣称
完成；可以先用合成数据验证目录、清单和恢复逻辑。
