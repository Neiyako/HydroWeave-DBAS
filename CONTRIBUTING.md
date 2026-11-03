# 贡献指南 · Contributing

感谢你考虑为 DBAS 做贡献。本文说明本仓的边界、开发流程与验收标准。

> English summary at the bottom.

---

## 一、先确认边界

DBAS **只做数据管理与抓取**：

- ✅ 可以提：扫描、索引、校验、备份、任务编排、CDS 下载、Zarr 派生、报告
- ❌ 不要提：科研计算、物理量公式、误差分析、绘图——这些属于
  [HydroWeave 实验仓](https://github.com/Neiyako/HydroWeave)

判断标准：**这段代码回答的是"数据是什么/在哪/坏没坏/怎么取"，还是"数据算出来是多少"。**
前者进本仓，后者不进。

## 二、开发环境

```bash
git clone https://github.com/Neiyako/HydroWeave-DBAS.git
cd HydroWeave-DBAS
./scripts/setup.sh --all      # 装全部 extras
./bin/dbas --help
```

**一律用 `./bin/dbas` 启动**，不要用 `python -m dbas`（原因见
[部署文档](docs/deployment.md#四为什么必须用-bindbas)）。

## 三、提交前必须全部通过

```bash
.venv/bin/python -m ruff check src tests scripts   # 0 问题
.venv/bin/python -m mypy                           # 0 错误
.venv/bin/python -m pytest --cov                   # 全绿且覆盖率 ≥ 80%
```

三项都是**硬门禁**，CI 会逐项检查，任一失败即变红。提交前请在本地跑通，
不要依赖 CI 反馈。

### 测试要求

- 新功能必须带测试，且优先用**合成数据**（`tests/conftest.py` 的 `make_nc`/
  `make_volume`），不联网、不依赖真实 ERA5 卷；
- 断言要具体：不要 `pytest.raises(Exception)`（ruff `B017` 会拦），
  写明确的异常类型并检查消息；
- 修 bug 时，先写一个**能复现该 bug 的失败测试**，再改代码。

## 四、代码约定

- **类型标注**：新增函数写注解。mypy 目前不强制全部函数都有注解
  （`disallow_untyped_defs = false`），但 `src` 下不得引入新的类型错误；
- **异常处理**：不要用裸 `except:`；宽泛捕获必须写 `# noqa: BLE001` 并注明原因；
- **注释**：解释"为什么"而不是"做什么"。与既有行为不同的地方要写清取舍；
- **中文/英文**：注释与文档用中文，标识符用英文。两份 README 需同步更新。

## 五、提交信息

用中文，首行 ≤ 50 字概括改动，正文说明**为什么改**和**怎么验证的**：

```text
zarr: 修复 point_series 下纬度维分块越界

原实现按 min(chunk, size) 取块长但未处理 size 为 0 的空轴，
导致全缺测文件的导出失败。改为遇到空轴直接跳过该变量。

验证：新增 test_empty_axis_is_skipped，pytest 158→159 passed。
```

## 六、提交 PR

1. 从 `main` 拉分支：`fix/xxx`、`feat/xxx`、`docs/xxx`；
2. 一个 PR 只做一件事，不混入无关格式化；
3. PR 描述里写清：改了什么 / 为什么 / 如何验证 / 有无破坏性变更；
4. 等 CI 全绿再请求评审。

## 七、报告问题

用 [Issue 模板](https://github.com/Neiyako/HydroWeave-DBAS/issues/new/choose)，
并附上：

- 操作系统与 Python 版本（`./bin/dbas --version` 不可用时给 `python3 -V`）
- 完整命令与**完整报错原文**（不要只写"报错了"）
- `./bin/dbas status` 的输出（去掉敏感路径）

---

## English summary

DBAS covers **data management and acquisition only** — scanning, cataloging,
verification, backup, task orchestration, CDS downloads and Zarr derivation.
Research computation, error analysis and plotting belong to the
[HydroWeave](https://github.com/Neiyako/HydroWeave) repository.

Before submitting, all three gates must pass locally:

```bash
.venv/bin/python -m ruff check src tests scripts   # 0 issues
.venv/bin/python -m mypy                           # 0 errors
.venv/bin/python -m pytest --cov                   # all green, coverage >= 80%
```

New features require tests built on synthetic data (no network, no real ERA5).
Bug fixes should start with a failing test that reproduces the bug. Use one
branch and one concern per pull request.

Open an issue for anything else; include OS, Python version, the exact command
and the full traceback.
