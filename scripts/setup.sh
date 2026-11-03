#!/usr/bin/env bash
# DBAS 一键部署：创建独立 venv、安装依赖、自检。
#
# 用法:
#   ./scripts/setup.sh                 # 基础安装（核心 + 开发依赖）
#   ./scripts/setup.sh --with-zarr     # 额外安装数组层依赖（推荐）
#   ./scripts/setup.sh --with-fetch    # 额外安装 CDS 下载依赖
#   ./scripts/setup.sh --with-eda      # 额外安装 subset/画像所需 xarray/dask
#   ./scripts/setup.sh --all           # 以上全部
#
# 幂等：重复执行只会补齐缺失依赖，不会重建已有 venv。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_ZARR=0; WITH_FETCH=0; WITH_EDA=0
for arg in "$@"; do
  case "$arg" in
    --with-zarr)  WITH_ZARR=1 ;;
    --with-fetch) WITH_FETCH=1 ;;
    --with-eda)   WITH_EDA=1 ;;
    --all)        WITH_ZARR=1; WITH_FETCH=1; WITH_EDA=1 ;;
    -h|--help)    sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: ${arg}（用 --help 查看用法）" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

step "1/5 检查 Python"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || fail "找不到 $PY，请先安装 Python 3.11+"
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PY" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)' \
  || fail "需要 Python >= 3.11，当前 $PYVER"
echo "Python $PYVER ($("$PY" -c 'import sys;print(sys.executable)'))"

step "2/5 创建独立虚拟环境 .venv"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv || fail "创建 venv 失败（Debian/Ubuntu 需先 apt install python3-venv）"
  echo "已创建 .venv"
else
  echo ".venv 已存在，复用"
fi
VENV_PY=".venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY=".venv/Scripts/python.exe"   # Windows(Git Bash)
[ -x "$VENV_PY" ] || fail "找不到 .venv 内的 python"

step "3/5 升级打包工具并安装 DBAS"
"$VENV_PY" -m pip install --quiet --upgrade pip setuptools wheel \
  || fail "升级 pip 失败（检查网络或代理）"
"$VENV_PY" -m pip install --quiet -e '.[dev]' \
  || fail "安装 EDITABLE 包失败：多为 PyPI 网络受限。换镜像重试：$VENV_PY -m pip install -e '.[dev]' -i https://pypi.tuna.tsinghua.edu.cn/simple（详见 docs/deployment.md）"

[ "$WITH_ZARR"  = 1 ] && { step "安装数组层依赖 [zarr]";  "$VENV_PY" -m pip install --quiet -e '.[zarr]'  || fail "安装 [zarr] 失败"; }
[ "$WITH_EDA"   = 1 ] && { step "安装 subset 依赖 [eda]";   "$VENV_PY" -m pip install --quiet -e '.[eda]'   || fail "安装 [eda] 失败"; }
[ "$WITH_FETCH" = 1 ] && { step "安装下载依赖 [fetch]";     "$VENV_PY" -m pip install --quiet -e '.[fetch]' || fail "安装 [fetch] 失败"; }

step "4/5 处理 macOS 隐藏 .pth 问题"
# iCloud/同步工具会给 editable 安装的 .pth 打上 hidden 标志，Python 3.13 的
# site.py 会直接跳过它，导致 `No module named dbas`。bin/dbas 用 PYTHONPATH
# 绕过该问题；这里顺便尝试修复，失败不影响使用。
if [ "$(uname)" = "Darwin" ]; then
  if ls .venv/lib/python*/site-packages/*.pth 2>/dev/null | head -1 | xargs -I{} ls -lO {} 2>/dev/null | grep -q hidden; then
    echo "检测到 .pth 被标记 hidden，尝试修复..."
    chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null \
      && echo "已修复（若 iCloud 再次改回，继续用 ./bin/dbas 即可）" \
      || echo "修复失败，不影响：请用 ./bin/dbas 启动"
  else
    echo "未检测到 hidden 标记"
  fi
else
  echo "非 macOS，跳过"
fi

step "5/5 自检"
./bin/dbas --help >/dev/null 2>&1 || fail "./bin/dbas 不可用，请查看上面的报错"
echo "CLI 正常"

"$VENV_PY" -c 'import zarr' 2>/dev/null && echo "数组层依赖 zarr: 已安装" \
  || echo "数组层依赖 zarr: 未安装（需要时执行 ./scripts/setup.sh --with-zarr）"

printf '\n\033[32m部署完成。\033[0m下一步：\n'
printf '  ./bin/dbas --help\n'
printf '  .venv/bin/python scripts/demo/make_demo_volume.py   # 生成合成数据\n'
printf '  详见 docs/deployment.md\n'
