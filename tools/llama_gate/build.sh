#!/usr/bin/env bash
# 编 llama-gate —— llama.cpp 那一档的取数服务（常驻，systemd 管）。
#
# 为什么要有这个脚本：这几步原先散在两个地方（llama.cpp 的 build.sh + 源文件头部的
# g++ 命令），换机器时靠人记。而补丁本身**没进 llama.cpp 的 git**（未提交），一次 git checkout
# 就没了，编译产物却还在 —— 直到跑一次才发现图变了，而且不报错、只是静默画错。
#
# 用法：
#   tools/llama_gate/build.sh                 # 打补丁 + 需要时编库 + 编二进制
#   tools/llama_gate/build.sh --skip-lib      # 库已在，只打补丁 + 编二进制
#   tools/llama_gate/build.sh --rebuild-lib   # 强制重编库（换 ROCm / 换 llama.cpp 版本后）
#
# 环境变量：
#   LLAMA_ROOT   llama.cpp 检出位置，默认 ~/work/llama/llama.cpp
#
# 编出来的 llama-gate 不是自包含的：RUNPATH 指向 $LLAMA_ROOT/build/bin，那儿的 libllama /
# libmtmd / libggml 被删或挪走，服务就打不起来。

set -euo pipefail

LLAMA_ROOT="${LLAMA_ROOT:-$HOME/work/llama/llama.cpp}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PATCH="$REPO_ROOT/tools/llama_gate/qwen3vl-prenorm.patch"
OUT_BIN="$REPO_ROOT/tools/llama_gate/bin/llama-gate"

SKIP_LIB=0
REBUILD_LIB=0
for arg in "$@"; do
    case "$arg" in
        --skip-lib) SKIP_LIB=1 ;;
        --rebuild-lib) REBUILD_LIB=1 ;;
        *) echo "不认的参数：$arg（只认 --skip-lib / --rebuild-lib）" >&2; exit 2 ;;
    esac
done

if [ ! -f "$PATCH" ]; then
    echo "找不到补丁文件：$PATCH" >&2
    exit 1
fi
if [ ! -d "$LLAMA_ROOT" ]; then
    echo "找不到 llama.cpp 检出：$LLAMA_ROOT（用 LLAMA_ROOT=... 指定）" >&2
    exit 1
fi

cd "$LLAMA_ROOT"

# --- 1. 打补丁（幂等：已打过就跳过）---
if git apply --check "$PATCH" 2>/dev/null; then
    git apply "$PATCH"
    echo "[1/3] 补丁已打上"
elif git apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "[1/3] 补丁已打过，跳过"
else
    echo "补丁打不上：$PATCH" >&2
    echo "  llama.cpp 源码可能变过（换版本后要重新生成 patch）。" >&2
    echo "  当前 qwen3vl.cpp 的状态：" >&2
    git diff --stat -- src/models/qwen3vl.cpp >&2 || true
    exit 1
fi

# --- 2. 编 llama.cpp 库（ROCm，可能要几十分钟）---
LIB="build/bin/libllama.so"
NEED_LIB=0
if [ "$REBUILD_LIB" = 1 ]; then
    NEED_LIB=1
elif [ ! -f "$LIB" ]; then
    NEED_LIB=1
elif [ src/models/qwen3vl.cpp -nt "$LIB" ]; then
    # 补丁刚打上 / 源码更新过 —— 库是旧的，得重编
    NEED_LIB=1
fi
if [ "$NEED_LIB" = 1 ]; then
    if [ "$SKIP_LIB" = 1 ]; then
        echo "库要重编（$LIB 不在或比源码旧），但给了 --skip-lib。" >&2
        echo "dump_hidden 会链到旧库/链不上 —— 去掉这个参数，或先自己编库。" >&2
        exit 1
    fi
    if [ ! -f build.sh ]; then
        echo "llama.cpp 根目录没有 build.sh，自己 cmake 一份（ROCm: -DGGML_HIP=ON）。" >&2
        exit 1
    fi
    echo "[2/3] 编 llama.cpp 库（ROCm，可能要几十分钟）"
    bash build.sh
else
    echo "[2/3] 库已在且比源码新（$LIB），跳过（--rebuild-lib 强制重编）"
fi

# --- 3. 编 llama-gate ---
echo "[3/3] 编 llama-gate"
mkdir -p "$REPO_ROOT/tools/llama_gate/bin"
g++ -O1 -std=c++17 \
    -I include -I ggml/include -I tools/mtmd -I src \
    "$REPO_ROOT/tools/llama_gate/llama_gate.cpp" \
    -L "$LLAMA_ROOT/build/bin" -lllama -lmtmd -Wl,-rpath,"$LLAMA_ROOT/build/bin" \
    -o "$OUT_BIN"
echo "产物：$OUT_BIN"
echo "依赖（该指向 \$LLAMA_ROOT/build/bin）："
ldd "$OUT_BIN" | grep -E "libllama|libmtmd" || true
