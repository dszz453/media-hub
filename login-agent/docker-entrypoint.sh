#!/bin/sh
# =============================================================================
#  login-agent 入口脚本：先起一块 Xvfb 虚拟屏，再把控制权交给 CMD。
#
#  ── 为什么不用 xvfb-run ──────────────────────────────────────────────
#  Debian 的 /usr/bin/xvfb-run 用的是新版脚本，它靠「Xvfb 主动发 SIGUSR1」
#  来判定服务器已经就绪：
#
#      trap : USR1                        # 先装好信号处理器
#      (trap '' USR1; exec Xvfb ...) &    # 后台拉起 Xvfb
#      wait || :                          # 等 SIGUSR1，或者等子进程退出
#
#  但实测本镜像里的 Xvfb **从来不发送 SIGUSR1**。验证方法：写一个只装
#  `trap "echo GOT" USR1` 的探针，后台起 Xvfb，再 `wait`，跑满 6 秒信号
#  始终没来（Xvfb 本身活得好好的）。
#
#  后果就是 `wait` 永久阻塞在 rt_sigsuspend 上：
#    - Xvfb 已经在 :99 正常服务（/tmp/.X11-unix/X99 与 .X99-lock 都在）
#    - 但 xvfb-run 永远走不到第 200 行那句 `DISPLAY=... "$@"`，
#      uvicorn 根本没被拉起来
#    - 容器表现为「Up、日志全空、健康检查一直 starting、8100 拒绝连接」
#
#  所以这里改成显式启动 Xvfb + 等 socket 真正可连 + exec 目标命令，
#  全程不依赖任何信号握手，行为完全确定。
#
#  ── 其它注意点 ──────────────────────────────────────────────────────
#  1. 必须以 exec 收尾，让 uvicorn 顶到 PID 1；否则 docker stop 发的
#     SIGTERM 会被本脚本吃掉，容器要等超时才肯退。
#  2. Xvfb 是本脚本的后台子进程，exec 之后它的父进程变成 uvicorn，
#     不影响使用（它不需要被回收，会一直跑到容器结束）。
#  3. 不加 -auth：容器内只有本机 unix socket 连接，实测无 auth 也能连。
#     加 -nolisten tcp 关掉 TCP 监听，少开一个口子。
# =============================================================================
set -eu

SCREEN="${XVFB_SCREEN:-1366x900x24}"
START_TIMEOUT="${XVFB_START_TIMEOUT:-30}"

# ---- 挑一个空闲 display -----------------------------------------------------
if [ -n "${XVFB_DISPLAY:-}" ]; then
    DISPLAY_NUM="$XVFB_DISPLAY"
else
    DISPLAY_NUM=""
    n=99
    while [ "$n" -lt 130 ]; do
        if [ ! -e "/tmp/.X11-unix/X$n" ] && [ ! -e "/tmp/.X$n-lock" ]; then
            DISPLAY_NUM="$n"
            break
        fi
        n=$((n + 1))
    done
    if [ -z "$DISPLAY_NUM" ]; then
        echo "[entrypoint] 99-129 全被占用，找不到空闲 X display" >&2
        exit 1
    fi
fi

# ---- 启动 Xvfb --------------------------------------------------------------
echo "[entrypoint] 启动 Xvfb :$DISPLAY_NUM -screen 0 $SCREEN"
Xvfb ":$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# ---- 等就绪：进程还活着 + unix socket 能 connect ---------------------------
waited=0
while :; do
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "[entrypoint] Xvfb 进程已退出，日志如下：" >&2
        cat /tmp/xvfb.log >&2 || true
        exit 1
    fi

    if python - "$DISPLAY_NUM" <<'PY' 2>/dev/null
import socket
import sys

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(1)
sock.connect("/tmp/.X11-unix/X" + sys.argv[1])
sock.close()
PY
    then
        break
    fi

    waited=$((waited + 1))
    if [ "$waited" -ge "$((START_TIMEOUT * 4))" ]; then
        echo "[entrypoint] 等 Xvfb 就绪超过 ${START_TIMEOUT}s，日志如下：" >&2
        cat /tmp/xvfb.log >&2 || true
        exit 1
    fi
    sleep 0.25
done

# socket 可连只说明监听起来了，再稳一小下，避免 Chromium 首帧抢跑
sleep 1

export DISPLAY=":$DISPLAY_NUM"
echo "[entrypoint] Xvfb 就绪 DISPLAY=$DISPLAY (pid=$XVFB_PID)，exec: $*"

exec "$@"
