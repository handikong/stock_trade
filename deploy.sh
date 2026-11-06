#!/usr/bin/env bash
# Linux 服务器非 Docker 部署。始终以普通部署用户运行。
set -euo pipefail

fail() { printf '部署失败：%s\n' "$*" >&2; exit 1; }
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$root"
python_bin="${PYTHON_BIN:-python3}"
venv_python="$root/.venv/bin/python"
runtime="$root/data/deploy"
pidfile="$runtime/worker.pid"
logfile="$runtime/worker.log"
host=127.0.0.1
port=8000

for binary in git flock nohup "$python_bin"; do
    command -v "$binary" >/dev/null || fail "缺少 $binary"
done
[[ -d /proc/self ]] || fail "此脚本用于带 /proc 的 Linux 服务器"
[[ "$(git rev-parse --show-toplevel 2>/dev/null || true)" == "$root" ]] ||
    fail "请先 git clone 项目，再从该检出目录运行 deploy.sh"
"$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    fail "需要 Python 3.11 或更新版本"
"$python_bin" -c 'import sqlite3' ||
    fail "当前 Python 缺少 SQLite 支持，请安装发行版提供的完整 Python 3.11+，或用 PYTHON_BIN 指定它"

umask 077
mkdir -p -- "$runtime"
exec 9>"$runtime/deploy.lock"
flock -n 9 || fail "已有部署在进行"

printf '拉取当前分支的上游代码…\n'
git pull --ff-only

if [[ ! -e .env ]]; then
    # 应用使用默认 SQLite；有飞书需求时由用户填入真实配置。
    printf 'DATABASE_URL=\nPOSTGRES_HOST=\n' > .env
fi

printf '安装 Python 依赖…\n'
if [[ ! -x "$venv_python" ]]; then
    "$python_bin" -m venv "$root/.venv"
fi
"$venv_python" -m pip install --disable-pip-version-check -e ./research
printf '初始化数据库与首次部署的股票池…\n'
"$venv_python" -m stock_research.deploy_init

running() {
    local first
    kill -0 "$1" 2>/dev/null && IFS= read -r -d '' first 2>/dev/null < "/proc/$1/cmdline"
}

owned_worker() {
    local -a args=()
    [[ "$(readlink "/proc/$1/cwd" 2>/dev/null || true)" == "$root" ]] || return 1
    mapfile -d '' -t args 2>/dev/null < "/proc/$1/cmdline" || return 1
    [[ "${args[0]:-}" == "$venv_python" && "${args[1]:-}" == -m &&
       "${args[2]:-}" == uvicorn && "${args[3]:-}" == stock_research.api:app ]]
}

if [[ -f "$pidfile" ]]; then
    old_pid="$(<"$pidfile")"
    [[ "$old_pid" =~ ^[1-9][0-9]*$ ]] || fail "PID 文件无效，请检查 $pidfile"
    if running "$old_pid"; then
        owned_worker "$old_pid" || fail "PID $old_pid 不属于本项目后台，未停止该进程"
        printf '停止旧后台（PID %s）…\n' "$old_pid"
        kill -TERM "$old_pid"
        for ((attempt=0; attempt<30; attempt++)); do
            running "$old_pid" || break
            sleep 1
        done
        if running "$old_pid"; then
            fail "旧后台未在30秒内退出；未强杀或启动第二个进程"
        fi
    fi
    rm -- "$pidfile"
fi

# 端口可能由此前手工启动的旧进程占用；不按端口杀进程。
"$venv_python" - "$host" "$port" <<'PY' || fail "8000 端口不可用，请先停止已有服务"
import socket
import sys

with socket.socket() as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((sys.argv[1], int(sys.argv[2])))
PY

printf '启动新版后台…\n'
nohup "$venv_python" -m uvicorn stock_research.api:app \
    --host "$host" --port "$port" --workers 1 \
    </dev/null >> "$logfile" 2>&1 9>&- &
new_pid=$!
printf '%s\n' "$new_pid" > "$pidfile"

if ! "$venv_python" - "$host" "$port" "$new_pid" <<'PY'
import os
import socket
import sys
import time

host, port, pid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        break
    try:
        with socket.create_connection((host, port), timeout=0.5):
            sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit(1)
PY
then
    fail "后台未在30秒内监听 $host:$port；请查看 $logfile"
fi
owned_worker "$new_pid" || fail "监听进程与本次启动不匹配，请查看 $logfile"
printf '部署完成，后台 PID %s，入口 http://%s:%s\n日志：%s\n' "$new_pid" "$host" "$port" "$logfile"
