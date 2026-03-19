#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
$BASE/planner_run.sh task-role-demo-001 "Role wrapper demo" "Demonstrate planner/implementer/reviewer wrappers." openclaw workspace high medium implementer
$BASE/implementer_run.sh task-role-demo-001 openclaw workspace "role wrapper demo" "Wrapper-based execution started" "Wrapper-based execution path is active."
$BASE/reviewer_run.sh task-role-demo-001 openclaw workspace approved "Demo task reviewed successfully." memory-updater true
