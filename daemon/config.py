# /opt/bmas/daemon/config.py
import os

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://:bmas-redis-secret-2026@192.168.4.240:6379/0")

# LiteLLM
LITELLM_URL = os.getenv("LITELLM_URL", "http://192.168.4.240:4000/v1")
LITELLM_KEY = os.getenv("LITELLM_KEY", "sk-bmas-master-2026")

# Triage
TRIAGE_URL = os.getenv("TRIAGE_URL", "http://192.168.4.240:8001/v1")

# Agent endpoints
AGENT_ENDPOINTS = {
    "planner":  os.getenv("AGENT_1_URL", "http://192.168.4.103:8000"),
    "executor": os.getenv("AGENT_2_URL", "http://192.168.4.112:8000"),
    "auditor":  os.getenv("AGENT_3_URL", "http://192.168.4.122:8000"),
}

# Redlock
LOCK_TTL_MS = int(os.getenv("LOCK_TTL_MS", "300000"))  # 5 minutes — must exceed 3× TASK_TIMEOUT_SECONDS (3 sequential agent dispatches)
LOCK_RETRY_DELAY_MS = int(os.getenv("LOCK_RETRY_DELAY_MS", "200"))
