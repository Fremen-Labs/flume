import asyncio
from typing import Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

async def run_cmd_async(*args: str, cwd: str | None = None, timeout: float = 15.0) -> Tuple[int, str, str]:
    """Runs a shell command asynchronously with a timeout.
    
    Returns:
        tuple: (return_code, stdout_str, stderr_str)
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        
        stdout_str = stdout_bytes.decode(errors='replace').strip() if stdout_bytes else ""
        stderr_str = stderr_bytes.decode(errors='replace').strip() if stderr_bytes else ""
        
        return proc.returncode, stdout_str, stderr_str
        
    except asyncio.TimeoutError:
        logger.error(f"Command timed out after {timeout}s: {' '.join(args)}", exc_info=True)
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            logger.warning(f"Failed to kill timed-out process: {' '.join(args)}", exc_info=True)
        return -1, "", f"Timeout expired after {timeout}s"
    except Exception as e:
        logger.error(f"Command execution failed: {' '.join(args)}", exc_info=True)
        return -1, "", str(e)
