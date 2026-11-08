# 由 deploy.sh 或 update.sh 在持有部署锁后载入。
host="${STOCK_TRADE_HOST:-0.0.0.0}"
port=8000
[[ "$host" == 0.0.0.0 || "$host" == 127.0.0.1 ]] ||
    fail "STOCK_TRADE_HOST 只支持 0.0.0.0 或 127.0.0.1"
command -v nohup >/dev/null || fail "缺少 nohup"
[[ -x "$venv_python" ]] || fail "尚未部署，请先运行 bash deploy.sh"

# 保持下载和编译在停服务之前。旧版 update.sh 拉取后也会加载这一步。
source "$root/scripts/prepare-intraday.sh"
prepare_intraday

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

if ! "$venv_python" - 127.0.0.1 "$port" "$new_pid" <<'PY'
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
printf '后台已启动，PID %s，监听 %s:%s\n日志：%s\n' "$new_pid" "$host" "$port" "$logfile"
if [[ "$host" == 0.0.0.0 ]]; then
    printf '访问地址：http://服务器IP:%s\n' "$port"
fi
