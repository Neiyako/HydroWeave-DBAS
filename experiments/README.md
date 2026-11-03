# experiments — 科研实验层

实验层只消费 `data/catalog.db` 登记的数据卷，**不写原始数据**；每个实验一个
子目录，自带配置、入口脚本和 README，产物统一落到
`data/analysis/<experiment>/runs/<run_id>/`。

约定：

- 配置只有一个入口（`config.yaml`），卷 ID / 数据集 ID / 区域 / 年份 / 阈值
  全部可改，代码里不出现机器专属路径；
- 每次运行写 `manifest.json`：配置哈希、输入逻辑指纹、Git revision、关键包
  版本、完成年份、失败日期、产物 SHA-256；
- 年度/批处理产物先写 `.part` 再原子替换，支持中断后按 `--run-id` 续跑；
- 同 `--run-id` 的配置或输入指纹变化会直接报错，防止两套口径的产物混放。

现有实验：

| 实验 | 说明 |
|------|------|
| [`tp_cloud_water`](tp_cloud_water/) | 青藏高原云水资源代理指标（PW / RH500 / CFRAC）分年 EDA 与制图 |

通用运行前置（`experiments` 是项目根下的包，需要项目根在 `PYTHONPATH`）：

```bash
PYTHONPATH=src:. .venv/bin/python -m experiments.<name>.<entry> --help
```

`tests/` 里的 `test_tp_cloud_water_experiment.py` 使用
`scripts/demo/make_tp_cloud_water_fixture.py` 生成的合成卷做端到端验证，
不需要外接盘。
