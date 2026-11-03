# 变更日志 · Changelog

本文件记录 DBAS 的所有主要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.0.0] - 2026-11-03

首个稳定版本。DBAS 从实验仓剥离，定位收敛为**科研数据资产管理与任务编排**。

### 新增

- **Zarr 数组层（L2）**：把 `.nc` 派生成 Zarr store，支持四种查询模式的分块
  策略（`field_series` / `point_series` / `area_stats` / `level_profile`），
  分块由查询模式反推而非照抄公开数据集。实测单点序列在 `point_series` 下
  比 `field_series` 快 58×、比直接读 NetCDF 快 7.9×。
- **清单哈希**：为解决 Zarr 缺少"单文件"比特级证据的问题，用排序后的
  `路径 + size + mtime_ns + sha256` 清单计算 digest，写入 `files.fingerprint`
  （`manifest-sha256`）。`dbas zarr verify` 可对账，并能精确定位变更的 chunk。
- `dbas zarr` 子命令族：`plan` / `enqueue` / `run` / `verify` / `info`，
  复用 Runner 的预算、批次、重试与断点续跑语义。
- **一键部署脚本** `scripts/setup.sh`（幂等，支持 `--with-zarr` /
  `--with-eda` / `--with-fetch` / `--all`）。
- **部署文档** `docs/deployment.md`：从零到跑通，含预期输出与 8 类故障排查。
- **Agent 提示词** `docs/agent-prompts.md`：四段可直接粘贴的提示词，
  便于他人用 AI Agent 部署。
- **中英文 README**（`README.md` / `README.en.md`）。
- **CI 四类门禁**：`ruff` 静态检查、`mypy` 类型检查（40 个文件 0 错误）、
  `pytest` 三版本矩阵（3.11/3.12/3.13）带 **80% 覆盖率门槛**、
  以及真实端到端的 CLI 冒烟测试。

### 变更

- **核心库内联**：原 `dbas-core` 作为外部 git 依赖指向一个已不可访问的仓库，
  导致任何人 `clone` 后都无法安装。现内联至 `src/dbas_core/`，安装只依赖 PyPI。
- **边界收敛**：删除 `cloud_water_proxy.py`（可降水量、饱和水汽压、误差指标、
  公式审计），该部分属实验与误差分析层。`profile`（内容画像）与 `qc`
  （文件级质量规则）保留——它们回答"数据能不能用"，属数据管理。
- **配置**：`profile` / `analysis` 配置段合并为 `zarr` 段；
  `chunk_mb` 从分析层迁移至数组层。
- 全部文档命令统一为 `./bin/dbas`（原文档写 `python -m dbas`，
  本机因 `.pth` 被 macOS 标记 hidden 而不可用）。

### 修复

- **基础安装后 CLI 无法启动**：`analysis/__init__.py` 顶层导入 `subset`，
  而 `subset` 依赖属可选 extras 的 `dask`/`xarray`，导致只装 `[dev]` 时
  `ModuleNotFoundError`。改为 PEP 562 惰性导入。
- **纬度翻转不留痕**：`normalize_axes` 翻转降序纬度轴时不产生 notes，
  而经度归一化却会记录，导致产物纬度顺序被改动却无任何记录。已补提示。
- **`zip()` 长度不等静默截断**：`repository._d()` 与 `subset` 的分块计算
  补 `strict=True`，把隐含假设变成显式检查。
- **`except` 变量与业务变量重名**：`fetch/cds.py` 中 `except ... as e` 的 `e`
  与经度变量 `e` 重名（Python 3 会在 except 块结束时删除该变量），
  改为 `north/west/south/east`。
- **demo 脚本把 `--help` 当目录名**：会在当前目录建出 `./--help` 并写入
  合成文件。已加参数校验。
- `zarr_export/store.py` 中 `chunks.get()` 可能返回 `None` 参与乘法的隐患，
  已显式收窄类型。
- `.coverage` 等测试产物加入 `.gitignore`。

### 测试

- 测试数 62 → **158**，覆盖率 75% → **81.6%**（含分支）。
- 新增 `test_regions_subset.py`（区域定义、0–360 经度折算、跨 0° 经线
  相交判断、diskutil 降级、subset 真实裁切与幂等）。
- 新增 `test_netcdf_meta_edges.py`（文件缺失/无权限/损坏、空壳文件、
  无时间轴、非法 units、曲线网格）。
- 新增 `test_cli_commands.py`（zarr 命令族、db 维护、tasks、report、
  dataset、volume 列表，含篡改检测）。

### 移除

- `cloud_water_proxy.py`（见"变更"）。
- 对外部 `dbas-core` git 仓库的依赖。

---

## 版本说明

本项目在 1.0.0 之前为内部迭代阶段，未发布独立版本。
`0.1.0` 为初始开发版本号，未对外发布。
