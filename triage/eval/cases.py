"""
Curated evaluation test cases for the bMAS Semantic Triage Router.

Each entry is a (task_description, expected_tier) tuple. The expected tier
represents the consensus ground truth for routing purposes. Add new cases
by appending to the relevant tier section below.

Model Routing (determines ground-truth labeling):
  SIMPLE  → edge-node (Gemma 4 E4B, 4B, free, 4096 ctx)
            Can the smallest edge model handle this with zero reasoning?
  LIGHT   → Gemini Flash Lite (cheap cloud)
            Short output, light reasoning, but beyond a 4B edge model.
  MEDIUM  → Gemini Flash ($)
            Produces a working artifact (code, document, analysis).
  COMPLEX → Gemini Pro ($$)
            Multi-component design, research synthesis, full applications.

Complexity Dimensions Covered:
  • Reasoning depth (single-step → multi-chain)
  • Domain knowledge (general → specialized → cross-domain)
  • Output structure (value → list → artifact → system)
  • Ambiguity (deterministic → open-ended)
  • Compositionality (atomic → interdependent sub-tasks)
  • Bloom's cognitive level (Remembering → Creating)
  • Distractor susceptibility (clean → noisy context)
  • Constraint density (free-form → heavily constrained)
  • Input length variation (short prompts → long context with embedded tasks)

Test Suite: 117 cases (33 SIMPLE, 25 LIGHT, 31 MEDIUM, 28 COMPLEX)
"""

TEST_CASES: list[tuple[str, str]] = [

    # ═══════════════════════════════════════════════════════════════════════
    # SIMPLE — Edge model (Gemma 4B) can handle these.
    # Single-step, deterministic, or trivially derivable answers.
    # No reasoning needed — pure lookup, math, or mechanical transform.
    # ═══════════════════════════════════════════════════════════════════════

    # ── Factual lookups ──
    ("What is the capital of France?", "SIMPLE"),
    ("What HTTP status code means 'Not Found'?", "SIMPLE"),
    ("What is the chemical symbol for gold?", "SIMPLE"),
    ("How many bytes are in a kilobyte?", "SIMPLE"),

    # ── String / formatting operations ──
    ("Convert the string 'bMAS router' to uppercase.", "SIMPLE"),
    ("Format the date '2026-05-06' to read 'May 6, 2026'.", "SIMPLE"),
    ("Reverse the string 'distributed'.", "SIMPLE"),
    ("Remove all whitespace from the string 'hello world foo bar'.", "SIMPLE"),

    # ── Arithmetic / conversions ──
    ("What is 15% of 240?", "SIMPLE"),
    ("Convert 72 degrees Fahrenheit to Celsius.", "SIMPLE"),
    ("What is 2 to the power of 10?", "SIMPLE"),
    ("Convert 5 kilometers to miles.", "SIMPLE"),

    # ── Single-step data operations ──
    ("Sort the list [3, 1, 4, 1, 5] in ascending order.", "SIMPLE"),
    ("What is the maximum value in the list [42, 17, 89, 3, 56]?", "SIMPLE"),
    ("Count the number of vowels in the word 'encyclopedia'.", "SIMPLE"),
    ("Is the number 37 prime?", "SIMPLE"),

    # ── Domain lookups (still single-step, edge model knows these) ──
    ("What is the default port number for PostgreSQL?", "SIMPLE"),
    ("What does the HTTP header 'Content-Type' specify?", "SIMPLE"),
    ("What is the time complexity of binary search?", "SIMPLE"),

    # ── Boolean / yes-no judgments ──
    ("Is 'text/html' a valid MIME type?", "SIMPLE"),
    ("Does Python use zero-based indexing?", "SIMPLE"),

    # ── Distractor-laden lookups (noisy context, trivial task) ──
    ("I've been reading about various database engines and their internals "
     "for a project on distributed caching. Anyway, what is the square root of 144?", "SIMPLE"),
    ("My team just finished a three-month sprint on microservices migration "
     "and we're exhausted. Quick question: what is the RGB hex code for white?", "SIMPLE"),

    # ── Format / type checks ──
    ("What file extension is used for Python source files?", "SIMPLE"),
    ("How many bits are in a single byte?", "SIMPLE"),
    ("What is the CIDR notation for a subnet mask of 255.255.255.0, and how many usable host addresses does it provide?", "SIMPLE"),

    # ── NEW: bMAS agent tasks that are genuinely SIMPLE ──
    ("What Redis command is used to set a key with an expiration?", "SIMPLE"),
    ("What is the default port for a FastAPI uvicorn server?", "SIMPLE"),
    ("What systemd command restarts a service?", "SIMPLE"),

    # ── NEW: Long input, simple task (input length variation) ──
    ("Here is a server access log with 20 entries:\n"
     "2026-05-06T10:00:01Z 192.168.1.10 GET /api/health 200 12ms\n"
     "2026-05-06T10:00:02Z 192.168.1.11 POST /api/login 200 45ms\n"
     "2026-05-06T10:00:03Z 192.168.1.12 GET /api/users 200 23ms\n"
     "2026-05-06T10:00:04Z 192.168.1.10 GET /api/health 200 11ms\n"
     "2026-05-06T10:00:05Z 192.168.1.13 DELETE /api/users/5 403 8ms\n"
     "2026-05-06T10:00:06Z 192.168.1.10 GET /api/health 200 10ms\n"
     "2026-05-06T10:00:07Z 192.168.1.14 PUT /api/settings 200 67ms\n"
     "2026-05-06T10:00:08Z 192.168.1.15 GET /api/metrics 200 34ms\n"
     "2026-05-06T10:00:09Z 192.168.1.10 GET /api/health 200 12ms\n"
     "2026-05-06T10:00:10Z 192.168.1.16 POST /api/upload 201 234ms\n"
     "How many requests returned a 200 status code?", "SIMPLE"),


    # ═══════════════════════════════════════════════════════════════════════
    # LIGHT — Flash Lite can handle these (cheap cloud).
    # Short output, light reasoning, extraction, pattern generation.
    # Beyond what a 4B edge model can reliably do, but not a full artifact.
    # ═══════════════════════════════════════════════════════════════════════

    # ── Extraction ──
    ("Extract the email from this text: 'Contact us at support@example.com for help.'", "LIGHT"),
    ("Extract all URLs from this paragraph: 'Visit https://example.com or http://test.org for details.'", "LIGHT"),
    ("Extract the person's name and job title from: 'Dr. Sarah Chen, Chief Data Scientist at Acme Corp, will keynote.'", "LIGHT"),
    ("Pull out all the dollar amounts from: 'The project cost $1,200 for hardware and $450 for software licensing.'", "LIGHT"),

    # ── Translation / rewriting ──
    ("Translate 'The distributed swarm is operational' into Spanish.", "LIGHT"),
    ("Rewrite this sentence in passive voice: 'The engineer deployed the service.'", "LIGHT"),
    ("Convert this sentence to a question: 'Redis supports pub/sub messaging.'", "SIMPLE"),  # mechanical inversion
    ("Rephrase this for a non-technical audience: 'The API returned a 503 due to upstream timeout.'", "LIGHT"),

    # ── Short summaries ──
    ("Write a 2-sentence summary of what a load balancer does.", "LIGHT"),
    ("List the top 3 differences between TCP and UDP in bullet points.", "LIGHT"),
    ("In one sentence, explain what DNS does.", "LIGHT"),

    # ── Pattern generation ──
    ("Generate a regular expression to match a standard 10-digit US phone number.", "LIGHT"),
    ("Write a CSS selector that targets all paragraph elements inside a div with class 'content'.", "LIGHT"),
    ("Generate a glob pattern that matches all Python files in any subdirectory.", "LIGHT"),
    ("Write a .gitignore entry that excludes all log files in any subdirectory.", "LIGHT"),
    ("Write a cron expression that runs a job every Monday at 3 AM.", "LIGHT"),

    # ── Short-form generation ──
    ("Convert this JSON object to YAML: {\"name\": \"bMAS\", \"version\": \"1.0\", \"active\": true}.", "SIMPLE"),  # format transform

    # ── Lightweight classification ──
    ("Classify these HTTP methods as safe or unsafe: GET, POST, PUT, DELETE, HEAD.", "LIGHT"),
    ("Given the error 'ECONNREFUSED', explain in one sentence what it means and the most likely cause.", "LIGHT"),

    # ── Short comparisons / lists ──
    ("Given a list ['apple', 'banana', 'cherry'], join them with commas and wrap the result in square brackets.", "SIMPLE"),  # mechanical string op
    ("In 2-3 bullet points, what are the key differences between a stack and a queue?", "LIGHT"),
    ("List 3 pros of using SSH keys over password authentication.", "LIGHT"),

    # ── Distractor-laden extraction ──
    ("I have a complex Kubernetes deployment with 47 pods across 3 namespaces. "
     "From the following log line, extract just the timestamp and error code: "
     "'2026-05-06T14:32:01Z ERROR pod/api-gateway code=E4012 msg=upstream_timeout'.", "LIGHT"),

    # ── NEW: bMAS agent LIGHT tasks ──
    ("Summarize this Redis MONITOR output in one sentence: "
     "'1715000001.123 [0 192.168.4.103:45678] \"HSET\" \"bmas:public:tasks\" \"task-abc\" \"{...}\"'", "LIGHT"),
    ("Given this systemd status output: 'Active: active (running) since Mon 2026-05-06 10:00:00 UTC; 3h ago', "
     "extract the service uptime.", "LIGHT"),
    ("Rewrite this error message for a non-technical user: "
     "'ETIMEDOUT: connect ETIMEDOUT 192.168.4.240:6379'.", "LIGHT"),

    # ── NEW: Long input, light task (varying input length) ──
    ("Here is a JSON configuration file for a service:\n"
     "{\n"
     "  \"service\": \"bmas-daemon\",\n"
     "  \"version\": \"1.0.0\",\n"
     "  \"redis\": {\"host\": \"192.168.4.240\", \"port\": 6379, \"password\": \"secret\"},\n"
     "  \"litellm\": {\"url\": \"http://192.168.4.240:4000\", \"key\": \"sk-bmas-master\"},\n"
     "  \"triage\": {\"url\": \"http://192.168.4.240:8001\", \"model\": \"Qwen/Qwen3-1.7B\"},\n"
     "  \"agents\": [\n"
     "    {\"role\": \"planner\", \"url\": \"http://192.168.4.103:8000\"},\n"
     "    {\"role\": \"executor\", \"url\": \"http://192.168.4.112:8000\"},\n"
     "    {\"role\": \"auditor\", \"url\": \"http://192.168.4.122:8000\"}\n"
     "  ],\n"
     "  \"lock_ttl_ms\": 30000,\n"
     "  \"log_level\": \"INFO\"\n"
     "}\n"
     "List all the IP addresses mentioned in this config.", "LIGHT"),

    ("The following is a multi-paragraph project status report:\n\n"
     "Sprint 14 concluded on May 3rd with 87% velocity. The team completed the Redis Blackboard "
     "integration, LiteLLM gateway deployment, and initial triage router validation. The triage "
     "model achieved 88% accuracy on a 101-case test suite. Key blockers included intermittent "
     "CUDA graph compilation timeouts on the RTX 5060 Ti and a Redis AUTH mismatch that took 2 "
     "hours to diagnose. The Hermes Agent deployment across 3 Proxmox nodes is stable with all "
     "health checks passing. Next sprint priorities: bMAS Daemon deployment, Mission Control UI "
     "scaffolding, and triage accuracy improvement to >95%.\n\n"
     "Summarize this report in exactly 2 bullet points.", "LIGHT"),


    # ═══════════════════════════════════════════════════════════════════════
    # MEDIUM — Gemini Flash needed. Produces working artifacts.
    # Single-function coding, focused explanations, document drafting.
    # Beyond what Flash Lite can do reliably, but not full system design.
    # ═══════════════════════════════════════════════════════════════════════

    # ── Coding tasks (single function / endpoint) ──
    ("Write a Python FastAPI endpoint that handles user login and returns a JWT token.", "MEDIUM"),
    ("Write a SQL query to calculate the 7-day rolling average of user signups by region.", "MEDIUM"),
    ("Write a Python decorator that retries a function up to 3 times with exponential backoff.", "MEDIUM"),
    ("Implement a Redis-backed rate limiter in Python that allows 100 requests per minute per user.", "MEDIUM"),
    ("Write a Python function that validates an email address using regex and returns True or False.", "MEDIUM"),
    ("Implement a basic LRU cache in Python using collections.OrderedDict.", "MEDIUM"),

    # ── Technical explanations (require domain depth) ──
    ("Explain the tradeoff between read-heavy and write-heavy database indexes.", "MEDIUM"),
    ("Explain the difference between optimistic and pessimistic locking with a concrete example.", "MEDIUM"),
    ("Describe how a circuit breaker pattern works in microservices and when to use it.", "MEDIUM"),
    ("Explain the CAP theorem and give a real-world example for each of the three tradeoff combinations.", "MEDIUM"),
    ("Explain database normalization from 1NF through 3NF with a concrete example showing the schema at each stage.", "MEDIUM"),
    ("Name 3 advantages of using containers over virtual machines.", "MEDIUM"),

    # ── DevOps / document artifacts ──
    ("Draft a professional apology email to customers regarding a 2-hour service degradation.", "MEDIUM"),
    ("Write a pull request description for a change that migrates user auth from session cookies to JWT tokens.", "MEDIUM"),
    ("Write a runbook for responding to a database connection pool exhaustion alert.", "MEDIUM"),
    ("Write a Dockerfile for a Python 3.12 FastAPI application with multi-stage build for minimal image size.", "MEDIUM"),
    ("Write a GitHub Actions workflow that runs pytest on push to main and uploads coverage to Codecov.", "MEDIUM"),
    ("Write a single SQL INSERT statement to add a user named 'Alice' with email 'alice@example.com' to a 'users' table.", "MEDIUM"),

    # ── Data transformation / business logic ──
    ("Write a Python function that takes a list of transaction dicts with 'amount' and 'currency' keys, "
     "converts all amounts to USD using a provided exchange rate dict, and returns the total.", "MEDIUM"),
    ("Write a bash script that monitors disk usage on a Linux server and sends a Slack webhook alert if any partition exceeds 85%.", "MEDIUM"),

    # ── Debugging / root cause analysis ──
    ("Given this Python traceback: 'RecursionError: maximum recursion depth exceeded in comparison', "
     "explain the three most common causes and how to fix each one.", "MEDIUM"),
    ("A PostgreSQL query that normally takes 50ms is now taking 12 seconds after a table grew from 100K to 10M rows. "
     "What are the most likely causes and what diagnostic steps would you take?", "MEDIUM"),

    # ── Constrained creative output ──
    ("Write a technical blog post outline (title, 5 section headings with 1-sentence descriptions) "
     "about implementing graceful shutdown in a Go HTTP server.", "MEDIUM"),

    # ── Comparative evaluation ──
    ("Compare SQLite, PostgreSQL, and MySQL for a small team building a read-heavy analytics dashboard. "
     "Recommend one with justification.", "MEDIUM"),

    # ── Security-aware coding ──
    ("Write a Python function that hashes a password using bcrypt with a configurable work factor and "
     "a separate function that verifies a plaintext password against a stored hash.", "MEDIUM"),

    # ── Cross-concern integration (2 systems) ──
    ("Write a Python context manager that acquires a Redis distributed lock with a TTL, "
     "executes the wrapped code, and releases the lock on exit, handling exceptions gracefully.", "MEDIUM"),

    # ── Distractor-heavy technical task ──
    ("Our company uses a complex multi-cloud setup with AWS, GCP, and Azure across 14 regions "
     "with over 200 microservices. Ignoring all that context, write a Python function that "
     "implements binary search on a sorted list and returns the index or -1 if not found.", "MEDIUM"),

    # ── NEW: bMAS agent MEDIUM tasks ──
    ("Write a Python async function that subscribes to a Redis Pub/Sub channel 'bmas:logs:*' "
     "using pattern matching and prints each message with a timestamp.", "MEDIUM"),
    ("Write a Python function that takes a Hermes Agent JSON response, validates it against "
     "a Pydantic schema with fields task_id (str), status (enum: completed/failed/partial), "
     "confidence (float 0-1), and result (str), and returns the validated model.", "MEDIUM"),
    ("Write a systemd service unit file for a Python FastAPI application that: starts after "
     "network and docker, restarts on failure with 5-second delay, uses a virtual environment, "
     "and logs to journald.", "MEDIUM"),

    # ── NEW: Long input, medium task ──
    ("Here is the output of 'docker compose logs bmas-triage --tail 50':\n\n"
     "bmas-triage | INFO: Started server process [1]\n"
     "bmas-triage | INFO: Waiting for application startup.\n"
     "bmas-triage | INFO: Application startup complete.\n"
     "bmas-triage | INFO: Uvicorn running on http://0.0.0.0:8000\n"
     "bmas-triage | WARNING: CUDA graph compilation took 3.2s (expected <1s)\n"
     "bmas-triage | ERROR: OOM when allocating KV cache for seq 47\n"
     "bmas-triage | INFO: Evicting 12 sequences from KV cache\n"
     "bmas-triage | WARNING: Request queue depth: 23 (threshold: 10)\n"
     "bmas-triage | ERROR: Request timeout after 30s for prompt_tokens=4891\n"
     "bmas-triage | INFO: GPU memory usage: 15.2/16.0 GB (95%)\n\n"
     "Analyze these logs, identify the root cause of the errors, and recommend "
     "specific configuration changes to fix them.", "MEDIUM"),


    # ═══════════════════════════════════════════════════════════════════════
    # COMPLEX — Gemini Pro needed. Multi-component architecture,
    # full applications, research synthesis. Requires frontier reasoning.
    # ═══════════════════════════════════════════════════════════════════════

    # ── System architecture ──
    ("Design a microservices architecture for a real-time trading platform with sub-millisecond latency, including database schema and failure recovery.", "COMPLEX"),
    ("Design a multi-tenant SaaS billing system with usage-based pricing, Stripe integration, invoice generation, and audit logging.", "COMPLEX"),
    ("Architect a real-time collaborative document editor like Google Docs, including CRDT selection, WebSocket layer, and conflict resolution strategy.", "COMPLEX"),
    ("Design the backend architecture for a ride-sharing platform handling 10 million daily rides, including geospatial matching, surge pricing, and driver payout systems.", "COMPLEX"),

    # ── Full application development ──
    ("Write a high-performance, concurrent web scraper in Go that manages rotating proxies, handles rate limits, and writes to PostgreSQL.", "COMPLEX"),
    ("Develop a custom PyTorch model from scratch that implements a Transformer-based time-series anomaly detection algorithm.", "COMPLEX"),
    ("Build a distributed task queue with exactly-once delivery guarantees, dead letter queues, priority scheduling, and horizontal scaling.", "COMPLEX"),
    ("Build a complete CLI tool in Rust that parses, validates, and transforms large CSV files (>1GB) with streaming, parallel column operations, and memory-safe error handling.", "COMPLEX"),

    # ── Research synthesis ──
    ("Synthesize a literature review on the impact of reinforcement learning on dynamic supply chain optimization based on recent 2025 papers.", "COMPLEX"),
    ("Compare and contrast the safety alignment approaches of GPT-4, Claude 3, and Gemini 2, analyzing their constitutional AI, RLHF, and red-teaming methodologies.", "COMPLEX"),
    ("Write a technical survey of vector database architectures (HNSW, IVF, PQ) with benchmark comparisons for billion-scale nearest neighbor search.", "COMPLEX"),
    ("Analyze the tradeoffs between event sourcing and traditional CRUD for financial transaction systems, including compliance, auditability, and disaster recovery implications.", "COMPLEX"),

    # ── Multi-system integration ──
    ("Design an end-to-end ML pipeline for fraud detection including feature engineering, model training, A/B testing, monitoring, and automated retraining with concept drift detection.", "COMPLEX"),
    ("Architect a zero-downtime database migration strategy for moving 500M rows from PostgreSQL to CockroachDB while maintaining read/write availability and data consistency.", "COMPLEX"),
    ("Design a multi-region deployment strategy for a latency-sensitive API serving 50K RPS, including DNS-based routing, cache invalidation, and data replication topology.", "COMPLEX"),
    ("Build a custom Kubernetes operator in Go that manages the lifecycle of ML model deployments, including canary rollouts, automatic scaling based on inference latency, and GPU resource scheduling.", "COMPLEX"),

    # ── Deep compositional reasoning ──
    ("Design a distributed consensus protocol for a heterogeneous IoT mesh network where nodes have "
     "varying compute capabilities, intermittent connectivity, and Byzantine fault tolerance requirements. "
     "Include message format specification, leader election, and formal proof of liveness.", "COMPLEX"),
    ("Architect a privacy-preserving federated learning system for healthcare data across 50 hospitals, "
     "including differential privacy budgets, secure aggregation protocols, model versioning, "
     "HIPAA compliance controls, and a monitoring dashboard for training convergence.", "COMPLEX"),

    # ── Cross-domain synthesis ──
    ("Design a real-time financial risk engine that combines market data streaming (WebSocket), "
     "Monte Carlo VaR simulation, regulatory reporting (Basel III), and an alerting system with "
     "sub-second latency SLAs. Include the data model, compute architecture, and failure modes.", "COMPLEX"),
    ("Build a complete observability platform for a 200-node Kubernetes cluster: distributed tracing "
     "with OpenTelemetry, metric aggregation with PromQL, log correlation, anomaly detection using "
     "statistical models, and an SLO-based alerting framework with burn-rate windows.", "COMPLEX"),

    # ── Temporal / trend analysis ──
    ("Analyze the evolution of transformer attention mechanisms from 2017 to 2026, covering vanilla "
     "self-attention, sparse attention, linear attention, flash attention, and multi-head latent attention. "
     "For each, discuss computational complexity, memory footprint, and empirical quality tradeoffs.", "COMPLEX"),

    # ── Adversarial / security architecture ──
    ("Design a zero-trust API gateway for a financial services platform that handles mTLS termination, "
     "OAuth 2.1 token validation, request-level encryption, rate limiting with per-tenant quotas, "
     "WAF integration, and real-time threat scoring. Include the deployment topology and incident response runbook.", "COMPLEX"),

    # ── Full-stack with heavy constraints ──
    ("Build a real-time multiplayer game server in Rust using WebSockets that supports 10,000 concurrent "
     "connections, implements server-authoritative game state with client-side prediction, lag compensation, "
     "anti-cheat validation, and horizontal scaling via consistent hashing.", "COMPLEX"),

    # ── Evaluation framework ──
    ("Design a comprehensive LLM evaluation harness that tests models across 6 cognitive dimensions "
     "(factual recall, logical reasoning, code generation, creative writing, safety, multilingual), "
     "implements statistical significance testing, supports human-in-the-loop annotation, "
     "generates automated leaderboards, and handles contamination detection.", "COMPLEX"),

    # ── Infrastructure-as-Code ──
    ("Write a complete Terraform module for deploying a production-grade EKS cluster on AWS with: "
     "multi-AZ node groups, Karpenter autoscaling, IRSA for pod-level IAM, Istio service mesh, "
     "external-dns, cert-manager, Datadog integration, and a GitOps pipeline using ArgoCD. "
     "Include the module structure, variable definitions, and a disaster recovery playbook.", "COMPLEX"),

    # ── NEW: bMAS agent COMPLEX tasks ──
    ("Design the complete bMAS Daemon orchestration layer: implement the task lifecycle "
     "(submit → triage → plan → execute → audit → publish), including Redlock-based distributed "
     "locking, dynamic expert persona generation for complex tasks, parallel agent dispatch with "
     "timeout handling, debate synthesis, and a FastAPI interface with WebSocket streaming for "
     "the Mission Control UI. Include error recovery for agent failures and cost tracking.", "COMPLEX"),
    ("Architect a self-healing Proxmox cluster monitoring system that: collects metrics from "
     "all LXC containers and VMs via the Proxmox API, detects service degradation using "
     "statistical anomaly detection, automatically restarts failed services via SSH, "
     "implements escalation policies (restart → migrate → alert), and provides a real-time "
     "dashboard with historical trend analysis.", "COMPLEX"),

    # ── NEW: Long input, complex task ──
    ("Given the following complete bMAS architecture documentation:\n\n"
     "The system consists of: (1) Three Proxmox hosts each running LXC containers with Gemma 4 E4B "
     "models via llama-server on Vulkan, (2) An HP OMEN control plane running Redis, LiteLLM, vLLM "
     "with Qwen3-1.7B for triage, and the bMAS Python Daemon, (3) Three Hermes Agent nodes with "
     "Planner/Executor/Auditor roles, (4) A LiteLLM gateway routing to Gemini Pro (heavy), Gemini "
     "Flash (medium), Gemini Flash Lite (light), and edge nodes (simple).\n\n"
     "The current issues are: (a) Edge nodes have only 4096 token context due to 6GB VRAM, (b) "
     "Inter-agent debate logs can overflow this context, (c) The Auditor cleanup sometimes races "
     "with new task submissions, (d) Gemini API rate limits cause cascading failures during burst "
     "traffic, (e) The triage router achieves only 88% accuracy.\n\n"
     "Design a comprehensive solution addressing ALL five issues, including specific code changes, "
     "configuration updates, architectural modifications, and a phased rollout plan.", "COMPLEX"),
]

