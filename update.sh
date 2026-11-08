#!/usr/bin/env bash
# 日常更新：拉取代码，按盘中配置更新 Go 和行情程序，再重启后台。
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$root/scripts/deploy-lock.sh"

command -v git >/dev/null || fail "缺少 git"
[[ "$(git rev-parse --show-toplevel 2>/dev/null || true)" == "$root" ]] ||
    fail "请先 git clone 项目，再从该检出目录运行 update.sh"
[[ -x "$venv_python" ]] || fail "尚未部署，请先运行 bash deploy.sh"
printf '拉取当前分支的上游代码…\n'
git pull --ff-only

# 使用拉取后的 Go 版本及构建规则，构建成功才重启。
source "$root/scripts/restart-worker.sh"
