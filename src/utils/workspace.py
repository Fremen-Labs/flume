import os
from pathlib import Path

class WorkspaceInitializationError(Exception):
    """Raised when the FLUME_WORKSPACE path bounds are maliciously traversed or cannot be initialized."""
    pass

def resolve_safe_workspace() -> Path:
    """
    Resolves FLUME_WORKSPACE natively securely preventing basic absolute path traversals.
    Verifies that the target resides within an expected parent directory (default safe zone).
    """
    raw = os.environ.get("FLUME_WORKSPACE", "").strip()
    
    default_zone = Path.cwd() / "workspace"
    
    if not raw:
        return default_zone.resolve()
        
    target = Path(raw).resolve()
    
    parts = target.parts
    if parts == ('/',) or parts == ('\\',):
        raise WorkspaceInitializationError(f"Rejected sensitive system root path: {target}")
        
    if "/etc" in parts or "/var" in parts or "/root" in parts or "/usr" in parts or "/sys" in parts:
        raise WorkspaceInitializationError(f"CRITICAL: Path Traversal boundary violation. Targeted static system vector: {target}")
        
    if not str(target).startswith(str(Path.home().resolve())) and not str(target).startswith(str(Path.cwd().resolve())):
         raise WorkspaceInitializationError(f"CRITICAL: Target {target} escapes both the user home bounds and execution context bounds. To prevent file-system read/write traversals, restrict FLUME_WORKSPACE to localized directories.")

    return target
