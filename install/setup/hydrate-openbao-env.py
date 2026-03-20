#!/usr/bin/env python3
"""Print shell exports for ES_* after OpenBao hydration (used by create-es-indices.sh)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo layout: this file lives in install/setup/, src is ../../src from here when run from repo root
def main() -> None:
    workspace_root = Path(os.environ.get("FLUME_WORKSPACE_ROOT", "")).resolve()
    if not workspace_root.is_dir():
        print("echo 'hydrate-openbao-env: set FLUME_WORKSPACE_ROOT to Flume repo root' >&2", file=sys.stderr)
        sys.exit(1)
    src = workspace_root / "src"
    if not src.is_dir():
        src = workspace_root
    sys.path.insert(0, str(src))
    from flume_secrets import apply_runtime_config

    apply_runtime_config(src)
    keys = ("ES_API_KEY", "ES_URL", "ES_VERIFY_TLS")
    for k in keys:
        v = os.environ.get(k, "")
        if v:
            # Safe single-quoted export for bash
            safe = v.replace("'", "'\"'\"'")
            print(f"export {k}='{safe}'")


if __name__ == "__main__":
    main()
