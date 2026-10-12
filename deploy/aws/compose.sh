#!/usr/bin/env bash
# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Run docker compose on the box with the rendered env file (as root).
#   deploy/aws/compose.sh ps
#   deploy/aws/compose.sh exec -T api mavis telegram info
set -euo pipefail
# shellcheck source=deploy/aws/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
state_require
compose_remote "$@"
