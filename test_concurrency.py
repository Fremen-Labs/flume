import asyncio
import time
import httpx
import sys

async def fetch():
    start = time.time()
    async with httpx.AsyncClient() as client:
        try:
            await client.get("http://localhost:8765/api/snapshot", timeout=30)
        except Exception as e:
            print(f"Request failed: {e}")
    return time.time() - start

async def main():
    print("Spawning 5 concurrent requests against the Uvicorn daemon...")
    start = time.time()
    results = await asyncio.gather(*(fetch() for _ in range(5)))
    total = time.time() - start
    print(f"\n--- RESULTS ---")
    print(f"Total time for 5 concurrent requests: {total:.2f}s")
    for i, t in enumerate(results):
        print(f" Request {i+1}: {t:.2f}s")
    
    if total >= 14:
        print("\n[VULNERABILITY CONFIRMED]: ASGI Event Loop is strictly blocking concurrent executions linearly! (Expected ~3.5s, took >14s)")
    else:
        print("\n[MIGITATED]: FastApi/Uvicorn Threadpool is actively absorbing the blocking operations concurrently.")

if __name__ == "__main__":
    asyncio.run(main())
