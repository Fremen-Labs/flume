import os
import random
import httpx
import asyncio

async def _post_gateway_async(client: httpx.AsyncClient, path: str, payload: dict, timeout: int = 180, max_retries: int = 3) -> dict:
    url = f'{_gateway_url()}{path}'
    worker_name = os.environ.get('FLUME_WORKER_NAME', 'unknown')
    backoffs = [30, 60, 120]
    
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={'X-Worker-Name': worker_name},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 401, 402, 403, 404, 429):
                logger.error(f"Gateway rejected request (HTTP {e.response.status_code}): {e.response.text}")
                raise Exception(f"Gateway HTTP {e.response.status_code}: {e.response.reason_phrase}") from e
            if attempt < max_retries:
                base_sleep = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(f"Gateway HTTP error (attempt {attempt + 1}/{max_retries + 1}): {e}. Backing off {sleep_time:.1f}s.")
                await asyncio.sleep(sleep_time)
            else:
                logger.error(f"Gateway connection permanently failed: {e}")
                raise e
        except httpx.RequestError as e:
            if isinstance(e, httpx.ReadTimeout) and timeout >= 60:
                logger.error(f"Gateway request timed out after {timeout}s.")
                raise e
            if attempt < max_retries:
                base_sleep = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                jitter = base_sleep * 0.1 * (random.random() * 2 - 1)
                sleep_time = max(1.0, base_sleep + jitter)
                logger.warning(f"Gateway connection issue (attempt {attempt + 1}/{max_retries + 1}): {e}. Backing off {sleep_time:.1f}s.")
                await asyncio.sleep(sleep_time)
            else:
                logger.error(f"Gateway connection permanently failed: {e}")
                raise e
