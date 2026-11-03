# 用 AI Agent 部署 DBAS：提示词模板

复制下面任意一段提示词给你的 AI Agent（Claude Code、Cursor、Codex、DSH 等）。
每段都是**自包含**的：Agent 不需要读这份文件以外的上下文。

选哪段：

| 场景 | 用哪个 |
|---|---|
| 第一次在这台机器装 DBAS | 提示词 A |
| 已经装过，要接入真实数据盘 | 提示词 B |
| 装完但报错了 | 提示词 C |
| 要给数据建 Zarr 数组层并验证性能 | 提示词 D |

---

## 提示词 A：从零部署

```text
请在这台机器上部署 DBAS（一个科研数据资产管理系统，Python 项目）。

仓库：https://github.com/Neiyako/HydroWeave-DBAS.git
目标目录：~/HydroWeave-DBAS（若已存在则先问我是否覆盖）

按以下步骤执行，每一步做完向我报告结果，遇到失败先诊断再修，不要跳过：

1. 检查环境：python3 -V 是否为 3.11+，git 是否可用，磁盘剩余空间是否 >2GB。
2. clone 仓库到目标目录。
3. 运行 ./scripts/setup.sh --with-zarr 完成安装。
4. 验证 ./bin/dbas --help 正常输出。

关键约束（务必遵守，否则会踩坑）：
- 本项目在本机必须用 ./bin/dbas 启动，不要用 python -m dbas 或 .venv/bin/dbas。
  原因：macOS 同步工具会把 editable 安装的 .pth 标记为 hidden，Python 3.13 的
  site.py 会跳过它，导致 "No module named dbas"。bin/dbas 用 PYTHONPATH 绕过。
- 安装若报网络错误，是 PyPI 访问问题，可用清华镜像重试：
  .venv/bin/python -m pip install -e '.[dev]' -i https://pypi.tuna.tsinghua.edu.cn/simple
- 不要修改 src/ 下的任何代码。部署失败请报告错误原文，由我决定如何处理。

5. 完成后再跑一遍自检，把这三项结果贴给我：
   .venv/bin/python -m pytest                          # 预期 90 passed
   .venv/bin/python scripts/demo/make_demo_volume.py   # 生成合成数据
   ./bin/dbas zarr plan --source data/demo_volume/2023/era5_demo_00.nc
```

---

## 提示词 B：接入真实数据盘

```text
DBAS 已装好（启动命令是 ./bin/dbas）。现在要把它接到我的外接数据盘上。

数据盘路径：<填写实际路径，例如 /Volumes/ERA5HD>
卷 ID（自定，建议英文短名）：<填写，例如 era5hd>

请按顺序执行并报告每步输出：

1. 确认盘已挂载、可读：ls 该路径能看到文件。
2. 登记卷（ID 一旦定下不要改，它决定索引身份）：
   ./bin/dbas volume register <盘路径> --id <卷ID>
3. 先小规模盘点，确认文件数符合预期：
   ./bin/dbas scan --volume <卷ID> --mode inventory
4. 确认数量无误后，一次完成盘点与解析入队：
   ./bin/dbas scan --volume <卷ID> --mode inventory --enqueue-metadata
5. 小预算试跑解析，观察速率：
   ./bin/dbas scan --volume <卷ID> --mode metadata --budget 300 --batch-size 50
6. 查看进度：./bin/dbas status

注意事项：
- 第 3 步和第 4 步有顺序陷阱：--enqueue-metadata 只对"本次扫描中新增/变化"的文件
  入队。如果第 3 步已经跑过，文件已登记未变化，第 4 步不会再入队。此时直接执行
  ./bin/dbas scan --volume <卷ID> --mode metadata 即可处理已有文件。
- 第 5 步跑不完是正常的（预算上限），重跑同一命令会从断点继续。
- 如果 DATA 盘很大，先告诉我总文件数，等我确认再全量跑。
- 绝对不要让 DBAS 写原始盘；它只读。
```

---

## 提示词 C：部署排错

```text
DBAS 部署出错了，请帮我诊断。

我的操作和报错原文：
<粘贴你执行的命令和完整报错>

已知的常见原因和对应处理：

1. "No module named dbas"
   → .pth 被 macOS 同步工具标记 hidden。改用 ./bin/dbas。
   想修复 console script：chflags nohidden .venv/lib/python3.*/site-packages/*.pth

2. "No module named 'dask'" 或 'xarray'
   → 这些是可选依赖。数组层需要：./scripts/setup.sh --with-zarr
     区域裁切需要：./scripts/setup.sh --with-eda

3. pip 安装报网络/超时
   → PyPI 访问问题，换镜像：
     .venv/bin/python -m pip install -e '.[dev]' -i https://pypi.tuna.tsinghua.edu.cn/simple

4. "error: command 'gcc' failed" 或缺 HDF5
   → netCDF4 没找到预编译 wheel。macOS: brew install hdf5；
     Debian/Ubuntu: apt install libhdf5-dev python3-dev

5. "挂载点不存在或不是目录"
   → volume register 要求路径当前真实存在。外接盘未挂载时无法注册。

6. 从旧版升级后报 "No module named 'dbas_core'"
   → 核心库已内联进 src/dbas_core/。重装一次：
     .venv/bin/python -m pip uninstall -y dbas-core
     .venv/bin/python -m pip install -e '.[dev]'

请先判断属于哪一种，再给修复步骤；判断不了就把完整环境信息收集给我：
python3 -V、uname -a、.venv/bin/python -m pip list、报错前后完整日志。
不要自行修改项目源码。
```

---

## 提示词 D：建 Zarr 数组层并验证收益

```text
DBAS 已装好并已索引了数据卷 <卷ID>（启动命令 ./bin/dbas）。
现在要给这批数据建 Zarr 数组层，并验证它是否真的提升了查询效率。

背景（决定怎么选分块策略）：
- 分块方案由「查询模式」决定，不是越大越好。四种：
  field_series   整场单时刻查询（默认）
  point_series   单点/少数站点的长时间序列
  area_stats     区域平均、时间统计
  level_profile  按气压层做垂直剖面
- 选错布局会比不建还慢。实测同一份数据，单点序列用 point_series 比 field_series
  快 58×；但整场查询反过来慢 50×。

我的主要查询方式是：<描述，例如"读取某个格点 1980-2024 的整段时间序列">

请执行：

1. 先预览布局，不写数据：
   ./bin/dbas zarr plan --source <抽一个代表性 .nc 文件>
   把四种布局的分块方案贴给我，确认哪一种匹配我的查询方式。

2. 入队并执行（先用小批次观察）：
   ./bin/dbas zarr enqueue --volume <卷ID> --target <选定的target>
   ./bin/dbas zarr run --budget 900 --batch-size 5

3. 对账，确认 store 与登记指纹一致：
   ./bin/dbas zarr info
   ./bin/dbas zarr verify --store <某个store相对路径>
   预期输出 "✔ 一致"。

4. 实测性能对比（这一步很重要，不要跳过）：
   用同一份数据，分别测「直接从 .nc 读」与「从 .zarr 读」同一查询的耗时，
   各跑 5 次取最小值，把对比表贴给我，并明确说明 Zarr 在我的查询模式下
   是更快还是更慢。如果更慢，如实告诉我——Zarr 不是万灵药，
   整场顺序读取本来就是 NetCDF 的强项。

注意：
- 跑不完是正常的，重跑同一命令会从断点续跑。
- 数组层是派生物，原始 .nc 一个字节都不动，store 随时可删可重建。
```

---

## 给 Agent 的通用约定（可附在任何提示词后）

```text
通用规则：
- 启动一律用 ./bin/dbas，不要用 python -m dbas。
- 不要修改 src/ 与 tests/ 下的代码；那是项目源码，不是部署配置。
- 不要删除 data/ 以外的东西；data/ 是运行产物目录，可安全清理重建。
- 每一步都报告实际输出，不要只说"已完成"。失败时贴完整报错，不要自己编原因。
- 涉及删除、覆盖、推送的操作，先问我。
```

---

## 项目速查（Agent 可能需要）

| 项 | 值 |
|---|---|
| 启动命令 | `./bin/dbas`（本项目根目录下） |
| 安装脚本 | `./scripts/setup.sh --with-zarr` |
| 配置文件 | `configs/system.yaml` |
| 运行产物 | `data/`（catalog.db、reports、backups、arrays） |
| 测试 | `.venv/bin/python -m pytest`（90 passed） |
| 文档 | `docs/deployment.md`（部署）、`docs/architecture.md`、`docs/zarr-layer.md` |
| Python | 3.11+ |
| 核心库 | 已内联在 `src/dbas_core/`，无需单独安装 |
