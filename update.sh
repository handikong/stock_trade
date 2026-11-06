#!/usr/bin/env bash
# 复用首次部署流程，已有数据库和名单会保留。
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$root/deploy.sh" "$@"
