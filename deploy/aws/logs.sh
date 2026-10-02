#!/usr/bin/env bash
# logs.sh [service ...] [-f]   (default: last 200 lines of everything)
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
state_require
follow=()
svcs=()
for a in "$@"; do
  if [[ "$a" == "-f" ]]; then follow=(-f); else svcs+=("$a"); fi
done
compose_remote logs --tail 200 ${follow[@]+"${follow[@]}"} ${svcs[@]+"${svcs[@]}"}
