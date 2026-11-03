# 部署文档：从零到跑通

面向第一次拿到本仓库的人。按顺序执行即可；每一步都给出**预期输出**，对不上就跳到文末「故障排查」。

支持环境：macOS、Linux、Windows(WSL)；Python 3.11+。

---

## 零、前置条件

| 项目 | 要求 | 检查命令 |
|---|---|---|
| Python | 3.11 及以上 | `python3 -V` |
| Git | 任意近版本 | `git --version` |
| 磁盘 | 基础安装约 600 MB（含 venv） | — |
| 网络 | 首次安装需访问 PyPI 与 GitHub | — |

`netCDF4` 有预编译 wheel，常见的 macOS/Linux x86_64 与 Apple Silicon、Windows 平台都不需要单独装 HDF5 或编译工具链。若你的平台没有 wheel，需要 `apt install libhdf5-dev` / `brew install hdf5` 后再装。

## 一、获取代码

```bash
git clone https://github.com/Neiyako/HydroWeave-DBAS.git
cd HydroWeave-DBAS
```

## 二、一键安装（推荐）

```bash
./scripts/setup.sh --with-zarr
```

脚本会依次：检查 Python 版本 → 创建 `.venv`（已存在则复用）→ 升级 pip → 安装核心与开发依赖 → 可选安装数组层依赖 → 处理 macOS 的 `.pth` 隐藏位问题 → 自检 `./bin/dbas`。

**预期输出的最后几行**：

```text
==> 5/5 自检
CLI 正常
数组层依赖 zarr: 已安装

部署完成。下一步：
  ./bin/dbas --help
```

附加依赖按需选择：`--with-zarr`（数组层，推荐）、`--with-eda`（subset 与画像所需的 xarray/dask）、`--with-fetch`（CDS 下载）、`--all`（全部）。

脚本是幂等的，重复执行只补齐缺失项。

## 三、手动安装（等价于上面的脚本）

想看清每一步，或脚本在你的环境不适用时：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install -e '.[zarr]'    # 可选：数组层
./bin/dbas --help
```

Windows 下把 `.venv/bin/python` 换成 `.venv/Scripts/python.exe`，`./bin/dbas` 换成
`PYTHONPATH=src .venv/Scripts/python -m dbas`（Git Bash 里 `bin/dbas` 可直接用）。

## 四、为什么必须用 `./bin/dbas`

`pip install -e .` 会在 site-packages 里放一个 `.pth` 文件指向 `src/`。**iCloud、Dropbox、
OneDrive 等同步工具会给文件打上 macOS 的 `hidden` 标志**，而 Python 3.13 的 `site.py`
会静默跳过隐藏的 `.pth`，结果是：

```text
$ .venv/bin/python -m dbas status
No module named dbas
```

`bin/dbas` 用 `PYTHONPATH=src` 启动，与该标志位无关，所以始终可用。

想恢复 `.venv/bin/dbas`（console script）：

```bash
ls -lO .venv/lib/python3.*/site-packages/*.pth | grep hidden
chflags nohidden .venv/lib/python3.*/site-packages/*.pth
```

但同步工具可能在几分钟后再次改回，因此**建议长期使用 `./bin/dbas`**。

## 五、验证安装：跑通演示链路

不联网、不碰真实数据，用合成 NetCDF 走完整链路：

```bash
# 1) 生成合成数据（5 个文件，写入 data/demo_volume/）
.venv/bin/python scripts/demo/make_demo_volume.py

# 2) 登记数据卷 + 一次完成盘点与解析入队
./bin/dbas volume register data/demo_volume --id demo
./bin/dbas scan --volume demo --mode inventory --enqueue-metadata

# 3) 执行解析任务
./bin/dbas scan --volume demo --mode metadata
```

**预期输出**：`认领 5 | 成功 5 | 失败 0`。

```bash
# 4) 查看目录状态：解析状态应为 {'parsed': 5}
./bin/dbas status
```

```bash
# 5) 派生 Zarr 数组层并核对证据
./bin/dbas zarr enqueue --volume demo --target point_series
./bin/dbas zarr run
./bin/dbas zarr info
```

**预期输出**：`zarr info` 列出 5 个 `.zarr` 条目，每项带 `manifest-sha256:...` 指纹。

```bash
# 6) 对账：应输出 ✔ 一致
./bin/dbas zarr verify --store point_series/2023/era5_demo_00.zarr
```

```bash
# 7) 数据库自检与备份
./bin/dbas db check
./bin/dbas db backup
```

到此安装与全部核心能力验证完毕。

## 六、运行测试

```bash
.venv/bin/python -m pytest
```

**预期**：`158 passed`，且输出 `Required test coverage of 80.0% reached`。全部使用临时目录与合成数据，不联网、不需要真实 ERA5 卷。

只跑数组层：`./.venv/bin/python -m pytest -q tests/test_zarr_export.py`（27 passed）。

## 七、接入真实数据盘

```bash
# 用稳定的卷 ID；换挂载点后仍用同一个 ID
./bin/dbas volume register /Volumes/ERA5HD --id era5hd

# 盘点并解析（只对新增/变化的文件入队）
./bin/dbas scan --volume era5hd --mode inventory --enqueue-metadata
./bin/dbas scan --volume era5hd --mode metadata --budget 600 --batch-size 50

# 内容理解与质量证据
./bin/dbas dataset assign --volume era5hd
./bin/dbas profile --volume era5hd --limit 100
./bin/dbas qc run --volume era5hd

# 数组层
./bin/dbas zarr enqueue --volume era5hd --target point_series
./bin/dbas zarr run --budget 900 --batch-size 5

# 维护
./bin/dbas verify --volume era5hd
./bin/dbas db check && ./bin/dbas db backup
```

要点：

- `--budget` 是单次运行的秒数上限，`--batch-size` 是单次处理文件数上限。跑不完是正常的，**重跑同一命令即可从断点继续**；
- 首次接入大卷时先用小 `--budget` 观察速率，再放开；
- `data/` 与原始卷分离：DBAS 从不写原始卷。

## 八、CDS 下载（可选）

1. 在 [CDS](https://cds.climate.copernicus.eu/) 注册并接受数据集条款；
2. 把 URL 与 API key 写入 `~/.cdsapirc`（**不要**提交到仓库）；
3. 安装依赖并运行：

```bash
./scripts/setup.sh --with-fetch
./bin/dbas fetch plan    --config configs/fetch/era5_demo.yaml
./bin/dbas fetch enqueue --config configs/fetch/era5_demo.yaml
./bin/dbas fetch run     --config configs/fetch/era5_demo.yaml
```

下载成功的文件会写入 `downloads` 卷并自动入队元数据解析。

## 九、项目根与配置

- 项目根默认按 `pyproject.toml` 位置自动定位；不在仓库内执行时用 `--project-root <路径>` 显式指定；
- 系统配置在 `configs/system.yaml`（调度预算、资源水位、扫描扩展名、数组层分块参数等）；
- 所有产物写入项目侧 `data/`：`catalog.db`、`reports/`、`backups/`、`arrays/`、`downloads/`。该目录不入 Git。

## 十、故障排查

### `No module named dbas`

`.pth` 被同步工具标记为 hidden。用 `./bin/dbas`，或执行第四节的 `chflags nohidden`。

### 从旧版升级后报 `No module named 'dbas_core'`

旧版把核心算法库 `dbas-core` 作为外部 git 依赖安装；新版已把它**内联**进
`src/dbas_core/`。若你在旧环境上直接更新代码，site-packages 里的旧副本可能与
源码不一致，重装一次即可：

```bash
.venv/bin/python -m pip uninstall -y dbas-core
.venv/bin/python -m pip install -e '.[dev]'
```

验证导入来源应在 `src/` 内：

```bash
PYTHONPATH=src .venv/bin/python -c "import dbas_core;print(dbas_core.__file__)"
# 预期 .../HydroWeave-DBAS/src/dbas_core/__init__.py
```

### `pip install` 报网络错误

新版**不再**从 GitHub 拉取 `dbas-core`，安装只依赖 PyPI。若仍失败，通常是
PyPI 访问受限，可换镜像：

```bash
.venv/bin/python -m pip install -e '.[dev]' -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### `error: command 'gcc' failed` / 缺少 HDF5

你的平台没有 `netCDF4` 预编译 wheel。macOS：`brew install hdf5`；Debian/Ubuntu：
`apt install libhdf5-dev python3-dev`。或改用 conda 环境安装 `netcdf4`。

### `scan` 报「挂载点不存在或不是目录」

`volume register` 要求路径**当前真实存在**。外接盘未挂载时无法注册。挂载后重试；
已注册的卷在盘离线时会被标记为 offline，不会丢索引。

### `--enqueue-metadata` 之后任务队列仍为空

该标志**只对本次扫描中新增或变化的文件**入队。如果你先跑过一次不带该标志的
`inventory`，文件已登记且未变化，就不会再入队。此时直接：

```bash
./bin/dbas scan --volume <vol> --mode metadata
```

它会处理库里已有的 `unparsed` 文件。

### `zarr` 命令报 `No module named 'zarr'`

```bash
./scripts/setup.sh --with-zarr
```

### 导入 zarr 时出现 `ZarrUserWarning: Consolidated metadata ...`

zarr v3 的合并元数据仍是实验特性，**不影响读写**。介意可在 `configs/system.yaml`
把 `zarr.consolidate` 设为 `false`。

### 任务卡在 `running`

超过 `scheduler.stale_running_s`（默认 600 秒）无心跳会被自动回收为 `pending`，
重跑即可。慢盘/大文件场景应在 `configs/system.yaml` 上调大该值，而不是关掉回收。

### 数据库损坏

```bash
./bin/dbas db check                      # integrity_check + foreign_key_check
cp data/backups/catalog_<最新>.db data/catalog.db   # 用备份恢复
```
