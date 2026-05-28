#!/usr/bin/env bash
# check-direct-logs.sh
#
# Lightweight guard (step 4 of centralized logger uplift) that flags direct
# console.* / fmt.Print* / log.Print* / "raw slog" usage outside the blessed
# logger packages.
#
# Usage:
#   ./scripts/check-direct-logs.sh
#   ./scripts/check-direct-logs.sh --fix   # (future: suggest/apply imports + replacements)
#
# Exit non-zero on findings (CI friendly). Excludes the logger impls themselves
# and generated/node_modules.
#
# This is intentionally a fast grep-based codemod stub. For production, wire
# it to eslint --rule 'no-console: error' (frontend) + golangci-lint or
# custom analyzer (Go), and/or extend Logloom lint with a "no-raw-log" rule.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VIOLATIONS=0

echo "==> Checking for direct console.* usage in frontend (outside logger impls)..."
if grep -r --include='*.ts' --include='*.tsx' \
     -E '^\s*console\.(log|warn|error|debug|info|dir|table)\s*\(' \
     --exclude-dir=node_modules \
     --exclude-dir=dist \
     src/ 2>/dev/null | grep -v 'src/src/utils/logger.ts' | grep -v 'src/lib/logger.ts' ; then
  echo "   ^^^ Found direct console.* (migrate to import from @/lib/logger or @/src/utils/logger)"
  VIOLATIONS=$((VIOLATIONS+1))
else
  echo "   OK (no stray console.* in app code)"
fi

echo
echo "==> Checking for raw fmt.Print* in Go (outside logger and tests)..."
if grep -rn --include='*.go' \
     -E '^\s*fmt\.(Print|Printf|Println|Errorf)\s*\(' \
     --exclude '*_test.go' \
     . 2>/dev/null \
     | grep -v '/internal/logger/' \
     | grep -v '/vendor/' \
     | grep -v 'scripts/check-direct-logs.sh' ; then
  echo "   ^^^ Found raw fmt.Print (use internal/logger helpers or injected *slog.Logger)"
  VIOLATIONS=$((VIOLATIONS+1))
else
  echo "   OK (no stray fmt.Print in non-test Go)"
fi

echo
echo "==> Checking for direct slog.New / log.New / \"log\" import (prefer flumelogger)..."
if grep -rn --include='*.go' \
     -E '(^import.*\"log\"|slog\.New\(|log\.New\()' \
     --exclude '*_test.go' \
     . 2>/dev/null \
     | grep -v '/internal/logger/' \
     | grep -v '/vendor/' ; then
  echo "   ^^^ Consider routing through internal/logger for redaction + Logloom enrichment"
  # Not a hard violation (some legitimate cases)
fi

echo
if [ "$VIOLATIONS" -gt 0 ]; then
  echo "==> Found $VIOLATIONS violation group(s). Run with context from Logloom graph for prioritized fixes."
  echo "    Recommended: logloom lint --languages go,ts <path> (once rule added)"
  exit 1
else
  echo "==> All clear. Direct logging usage is centralized. Great!"
  exit 0
fi
