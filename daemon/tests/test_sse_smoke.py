import asyncio
import httpx
import json

async def test_sse():
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Submit task
        resp = await client.post("http://localhost:9000/submit", json={"task": "test SSE"})
        resp.raise_for_status()
        data = resp.json()
        task_id = data["task_id"]
        print(f"Submitted task: {task_id}")

        # Get system events (just sample a bit)
        print("\n--- System Events ---")
        async with client.stream("GET", "http://localhost:9000/events/system") as response:
            count = 0
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    print(line)
                    count += 1
                    if count >= 1:
                        break
        
        # Get task events
        print("\n--- Task Events ---")
        async with client.stream("GET", f"http://localhost:9000/events/{task_id}") as response:
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                    print(f"Event: {event_type}")
                    if event_type in ("complete", "error"):
                        break

if __name__ == "__main__":
    asyncio.run(test_sse())
