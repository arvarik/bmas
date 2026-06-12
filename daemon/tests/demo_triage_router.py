import asyncio

from triage_router import TriageRouter


async def demo():
    router = TriageRouter()
    
    # Simple task → routed to edge node (free)
    r1 = await router.classify("What is 2 + 2?")
    print(f"Simple: {r1.complexity.value} → {r1.litellm_model}")
    
    # Complex task → routed to Gemini Pro (paid)
    r2 = await router.classify(
        "Design a microservices architecture for a real-time trading platform "
        "with sub-millisecond latency requirements, including database schema, "
        "API contracts, and failure recovery strategies."
    )
    print(f"Complex: {r2.complexity.value} → {r2.litellm_model}")
    
    await router.close()

asyncio.run(demo())
