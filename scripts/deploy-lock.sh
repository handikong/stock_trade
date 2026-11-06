# 由 deploy.sh 与 update.sh 共用：在更新、初始化和重启时避免并发执行。
fail() { printf '操作失败：%s\n' "$*" >&2; exit 1; }
cd -- "$root"
[[ -d /proc/self ]] || fail "此脚本用于带 /proc 的 Linux 服务器"
command -v flock >/dev/null || fail "缺少 flock"
venv_python="$root/.venv/bin/python"
runtime="$root/data/deploy"
pidfile="$runtime/worker.pid"
logfile="$runtime/worker.log"
umask 077
mkdir -p -- "$runtime"
exec 9>"$runtime/deploy.lock"
flock -n 9 || fail "已有部署或更新正在进行"
