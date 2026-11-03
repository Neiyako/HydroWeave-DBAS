# scripts 目录约定

只放**可执行入口**，不放业务逻辑。逻辑分别在 `src/dbas/`（通用能力）与
`experiments/`（具体实验），脚本只做参数转发或一次性维护动作。

| 子目录 | 放什么 | 判定标准 |
|---|---|---|
| `analysis/` | 实验命令行包装：转发到 `experiments/<name>/` 的 `main()` | 用户会反复敲它跑正式产物 |
| `demo/` | 合成数据卷、fixture 生成器：不碰真实数据 | 只用于演示、冒烟、测试准备 |
| `dev/` | 维护与验收：独立验收、迁移、诊断 | 你自己或 CI 用，不是日常分析入口 |

## analysis/

| 脚本 | 说明 |
|---|---|
| `tp_cloud_water_eda.py` | 兼容旧命令，转发到 `experiments.tp_cloud_water.eda`；配置与 run 目录见实验 README |
| `tp_cloud_water_plot.py` | 同上，转发到 `plot.py` |

包装入口自带 `sys.path` 注入，`env -u PYTHONPATH` 也能直接跑；
正式跑仍推荐 `PYTHONPATH=src:.` + `experiments/tp_cloud_water/*.py`。

## demo/

| 脚本 | 产出 |
|---|---|
| `make_demo_volume.py` | 最小合成卷（少量 NetCDF），给 CLI 冒烟用 |
| `make_demo_era_volume.py` | “类 ERA5”卷：全球网格、多层变量、三种坐标轴约定，给 subset 链路演示 |
| `make_tp_cloud_water_fixture.py` | 云水代理实验的全套 fixture：mini catalog + 37 层 q/t + 冰川掩膜 + config |

## dev/

| 脚本 | 说明 |
|---|---|
| `validate_real_eda.py` | 真实 ERA5 卷独立验收，输出 A–G 报告与 PASS/CONDITIONAL_PASS/FAIL |

## 新增脚本时

1. 先判断属于上面哪一类，不确定就放 `dev/`；
2. 业务逻辑写进 `src/dbas/` 或 `experiments/`，脚本保持 20 行以内；
3. 用 `Path(__file__).resolve().parents[N]` 定位项目根，不要依赖调用时的 cwd；
4. 在对应子目录的 README 或本文件表格里登记一行。
