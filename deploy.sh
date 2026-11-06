#!/usr/bin/env bash
# Linux 服务器首次部署；依赖发生变化时也可重新执行。
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$root/scripts/deploy-lock.sh"

python_bin="${PYTHON_BIN:-python3}"
command -v "$python_bin" >/dev/null || fail "缺少 $python_bin"
"$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    fail "需要 Python 3.11 或更新版本"
"$python_bin" -c 'import sqlite3' ||
    fail "当前 Python 缺少 SQLite 支持，请安装发行版提供的完整 Python 3.11+，或用 PYTHON_BIN 指定它"

if [[ ! -e .env ]]; then
    printf 'DATABASE_URL=\nPOSTGRES_HOST=\n' > .env
fi

printf '安装 Python 依赖…\n'
if [[ ! -x "$venv_python" ]]; then
    "$python_bin" -m venv "$root/.venv"
fi
"$venv_python" -m pip install --disable-pip-version-check -e ./research
printf '初始化数据库与首次部署的股票池…\n'
"$venv_python" -m stock_research.deploy_init

source "$root/scripts/restart-worker.sh"
