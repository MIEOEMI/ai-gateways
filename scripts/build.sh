#!/usr/bin/env bash
# ===========================================================================
# 一键构建三个镜像：trae2api-hub / workbuddy2api / gateway-dashboard
#
# 做法：把上游仓库拉到一个**固定的基线提交**，打上本仓库的补丁，再 docker build。
# 不 vendor 上游源码 —— 上游更新频繁，绑提交 + 补丁比拷一份快照更容易跟进。
#
#   ./scripts/build.sh              # 增量（复用 .build/ 下的克隆）
#   ./scripts/build.sh --clean      # 丢弃克隆与构建目录，从零构建
#   ./scripts/build.sh --no-cache   # docker build 不用缓存
#
# 环境变量：
#   TRAE_UPSTREAM   覆盖 Trae 上游地址
#   WB_UPSTREAM     覆盖 WorkBuddy 上游地址
#   BUILD_DIR       构建工作目录（默认 ./.build）
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/.build}"

# 补丁基线：补丁是针对这两个提交生成的。上游若已前进，补丁可能冲突 ——
# 脚本会先 `git apply --check`，冲突时明确报错而不是硬打一个半截补丁。
TRAE_UPSTREAM="${TRAE_UPSTREAM:-https://github.com/shuishuipingan/trae2api-hub.git}"
TRAE_BASE="cba75ce6e166e9fbeb1ae1b09131cb94fe8b1121"

WB_UPSTREAM="${WB_UPSTREAM:-https://github.com/3304711297/workbuddy2api.git}"
WB_BASE="f7a1f2d03003020da5864e2a9359cac8ec720aec"

CLEAN=0
NO_CACHE=""
for arg in "$@"; do
  case "$arg" in
    --clean)    CLEAN=1 ;;
    --no-cache) NO_CACHE="--no-cache" ;;
    -h|--help)  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "找不到 docker"
command -v git    >/dev/null || die "找不到 git"

[ "$CLEAN" = "1" ] && { say "清理 $BUILD_DIR"; rm -rf "$BUILD_DIR"; }
mkdir -p "$BUILD_DIR"

# 拉上游到指定提交（浅拉取，只取需要的那一个）。
#
# 三级降级，因为构建机的网络实在不可靠（实测 github.com 的 git 协议会
# gnutls_handshake 失败，但 api.github.com 正常）：
#   1. 本地已有该提交 → 直接用，不联网
#   2. 按 sha 浅拉取
#   3. 拉默认分支最新 + 显式警告（补丁可能冲突，下一步的 --check 会兜住）
fetch_at() {
  local url="$1" dir="$2" base="$3" want
  want="$(echo "$base" | cut -c1-7)"

  if [ -d "$dir/.git" ] && git -C "$dir" rev-parse -q --verify "$base^{commit}" >/dev/null 2>&1; then
    git -C "$dir" checkout -q --detach "$base"
    git -C "$dir" reset -q --hard "$base"
    git -C "$dir" clean -qfd
    echo "  $want（本地已有，跳过联网）" >&2
    git -C "$dir" rev-parse --short HEAD
    return 0
  fi

  if [ ! -d "$dir/.git" ]; then
    git init -q "$dir"
    git -C "$dir" remote add origin "$url"
  fi

  if git -C "$dir" fetch -q --depth 1 origin "$base" 2>/dev/null; then
    git -C "$dir" checkout -q --detach FETCH_HEAD
  elif git -C "$dir" fetch -q --depth 1 origin HEAD 2>/dev/null; then
    echo "  ⚠ 无法按提交号拉取 $want，已退回默认分支最新（补丁可能需要更新）" >&2
    git -C "$dir" checkout -q --detach FETCH_HEAD
  elif git -C "$dir" rev-parse -q --verify HEAD >/dev/null 2>&1; then
    echo "  ⚠ 网络不可用，沿用本地已有的提交" >&2
  else
    echo "  ✗ 拉取失败且本地无可用提交：$url" >&2
    return 1
  fi
  git -C "$dir" reset -q --hard FETCH_HEAD 2>/dev/null || git -C "$dir" reset -q --hard HEAD
  git -C "$dir" clean -qfd
  git -C "$dir" rev-parse --short HEAD
}

apply_patch() {
  local dir="$1" patch="$2"
  [ -f "$patch" ] || die "补丁不存在: $patch"
  git -C "$dir" checkout -q -- .
  if git -C "$dir" apply --check "$patch" 2>/dev/null; then
    git -C "$dir" apply "$patch"
    echo "  已应用 $(basename "$patch")"
  elif git -C "$dir" apply --reverse --check "$patch" 2>/dev/null; then
    echo "  补丁已在工作树中，跳过"
  else
    die "补丁冲突：$(basename "$patch")
  上游可能已改动相关文件。需要基于新的基线重新生成补丁。"
  fi
}

# --------------------------------------------------------------------------
say "1/3 Trae 网关"
# --------------------------------------------------------------------------
TRAE_DIR="$BUILD_DIR/trae2api-hub"
rev="$(fetch_at "$TRAE_UPSTREAM" "$TRAE_DIR" "$TRAE_BASE")"
echo "  上游提交 $rev"
apply_patch "$TRAE_DIR" "$ROOT/patches/trae2api-hub.patch"
docker build $NO_CACHE -t trae2api-hub:latest "$TRAE_DIR"
echo "  ✓ trae2api-hub:latest"

# --------------------------------------------------------------------------
say "2/3 WorkBuddy 网关"
# --------------------------------------------------------------------------
WB_DIR="$BUILD_DIR/workbuddy2api"
rev="$(fetch_at "$WB_UPSTREAM" "$WB_DIR" "$WB_BASE")"
echo "  上游提交 $rev"
apply_patch "$WB_DIR" "$ROOT/patches/workbuddy2api.patch"

# channel_guard.py 是**新增文件**，不在 diff 里（git diff 只覆盖已跟踪文件）
cp "$ROOT/patches/channel_guard.py" "$WB_DIR/channel_guard.py"
echo "  已放入 channel_guard.py"

# 上游仓库没有 Dockerfile/wheels，这里组一个构建上下文
WB_BUILD="$BUILD_DIR/_wb-build"
rm -rf "$WB_BUILD"
mkdir -p "$WB_BUILD"
cp "$WB_DIR"/*.py "$WB_DIR"/turing_helper.cjs "$WB_BUILD"/ 2>/dev/null \
  || die "拷贝上游模块失败（目录结构可能变了）"
# 上游若自带 tools/ 等子目录，一并带上（目前不需要，缺了也不影响主链路）
[ -d "$WB_DIR/tools" ] && cp -r "$WB_DIR/tools" "$WB_BUILD"/
cp -r "$ROOT/docker/wheels" "$WB_BUILD/wheels"
cp "$ROOT/docker/workbuddy.Dockerfile" "$WB_BUILD/Dockerfile"
docker build $NO_CACHE -t workbuddy2api:latest "$WB_BUILD"
echo "  ✓ workbuddy2api:latest"

# --------------------------------------------------------------------------
say "3/3 用量面板"
# --------------------------------------------------------------------------
docker build $NO_CACHE -t gateway-dashboard:latest \
  -f "$ROOT/docker/dashboard.Dockerfile" "$ROOT"
echo "  ✓ gateway-dashboard:latest"

say "构建完成"
docker images --format '  {{.Repository}}:{{.Tag}}\t{{.Size}}' \
  | grep -E 'trae2api-hub|workbuddy2api|gateway-dashboard' || true
printf '\n下一步：cp .env.example .env && 编辑 .env && docker compose up -d\n'
