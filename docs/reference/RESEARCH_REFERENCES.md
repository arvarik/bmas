# Research References

This document is the organized index of every external paper,
standard, and practice source that bmas design and code build on.
Each entry gives a short description and names the exact place where
bmas uses it. The plan-time audit narratives live in
[docs/plans/RESEARCH_RECORD.md](../plans/RESEARCH_RECORD.md). This
index stays current: add one entry in the matching category when a
new source shapes a design or an implementation.

Most 2026 architecture and evaluation papers remain preprints. Treat
their reported results as design evidence, not settled guarantees.

## 1. Multi-agent architecture and coordination

- [Exploring Advanced LLM Multi-Agent Systems Based on Blackboard
  Architecture (LbMAS)](https://arxiv.org/abs/2507.01701) — Defines a
  control unit, a shared blackboard, and an LLM agent group with
  planner, critic, cleaner, conflict-resolver, and decider roles.
  Used for: the Classic blackboard runtime design
  (`daemon/src/core/variants/classic.py`, `docs/CLASSIC_HARNESS.md`),
  including role selection, cleanup, and explicit finalization.
- [PatchBoard: Schema-Grounded State Mutation for Reliable and
  Auditable LLM Multi-Agent
  Collaboration](https://arxiv.org/abs/2605.29313) — Replaces
  free-form board writes with schema-validated patches for auditable
  state mutation. Used for: the PatchBoard runtime plan
  (`docs/plans/03-patchboard/`) and the patchboard variant contract.
- [AgentRoom: Concurrent Multi-Agent Coding in a CRDT-Backed Shared
  Workspace](https://arxiv.org/abs/2608.23740) — Studies concurrent
  agent edits over a shared workspace with conflict-free replicated
  data types. Used for: workspace concurrency considerations in the
  shared-foundation plan (`docs/plans/SHARED_FOUNDATION.md`).
- [StagedWorkspace: A Versioned Workspace for Knowledge-Work
  Agents](https://arxiv.org/abs/2608.18050) — Argues for versioned,
  staged workspace state instead of destructive edits. Used for: the
  immutable-revision doctrine across datasets, test revisions, and
  execution snapshots.
- [Multi-agent Collaboration with State Management
  (STORM)](https://arxiv.org/abs/2605.20563) — Examines explicit
  state management as the coordination backbone. Used for: the
  durable board store and projection design
  (`daemon/src/core/board_store.py`, `daemon/src/core/blackboard.py`).
- [When 20 Agents Fail to Sort
  (MAS-BENCH)](https://aclanthology.org/2026.findings-acl.1698/) —
  Shows coordination collapse on a simple distributed task as agent
  count grows. Used for: bounded agent counts and explicit
  coordination limits in runtime configuration.
- [Towards a Science of Scaling Agent
  Systems](https://arxiv.org/abs/2512.08296) — Measures when more
  agents help and when coordination overhead dominates. Used for: the
  capacity policy defaults (`daemon/src/benchmarks/capacity.py`) and
  effort profiles.

## 2. Multi-agent failure modes and reliability

- [Why Do Multi-Agent LLM Systems Fail?
  (MAST)](https://arxiv.org/abs/2503.13657) — A taxonomy of
  specification, inter-agent, and verification failures across
  systems. Used for: the failure-category vocabulary behind attempt
  `failure_category` values and the planned failure taxonomy
  (work package 3.5).
- [MAS-FIRE: Fault Injection and Recovery Evaluation for Multi-Agent
  Systems](https://arxiv.org/abs/2602.19843) — Evaluates recovery
  behavior under injected faults. Used for: the failpoint-driven
  crash-boundary tests (`daemon/src/core/failpoints.py`,
  `daemon/tests/test_benchmark_admission_recovery.py`).
- [Hallucination Cascade in Multi-Agent
  Systems](https://arxiv.org/abs/2606.07937) — Shows how one agent's
  fabrication propagates through a pipeline. Used for: evidence
  requirements on scorers and receipts, and the salience controls in
  the Classic runtime.
- [ACIArena: Toward Unified Evaluation for Agent Cascading
  Injection](https://arxiv.org/abs/2604.07775) — Benchmarks cascading
  prompt-injection across agent boundaries. Used for: the
  cascading-injection tests planned in the verification plan and the
  privacy boundary (`daemon/src/core/privacy` surfaces, security
  matrix).
- [PAC-BENCH: Evaluating Multi-Agent Collaboration under Privacy
  Constraints](https://arxiv.org/abs/2604.11523) — Measures
  collaboration quality when agents must withhold private data. Used
  for: the privacy-boundary utility tests in the verification plan.
- [VeriMAP: Planner-Defined Verification for Multi-Agent
  Plans](https://aclanthology.org/2026.eacl-long.353/) — Attaches
  machine-checkable verification conditions to plans. Used for: the
  proposal-decision and execution-envelope verification flow in the
  Foundation agent protocol.
- [Runtime Governance: Policies on
  Paths](https://arxiv.org/abs/2603.16586) — Enforces policy on
  execution paths at runtime instead of at review time. Used for:
  the effect-grant authority chain and `validate_before_transport`
  checks (`daemon/src/effect_service.py`).

## 3. Long-horizon agent evaluation

- [The Long-Horizon Task Mirage? Diagnosing Where and Why Agentic
  Systems Break (HORIZON)](https://arxiv.org/abs/2604.11978) —
  Diagnoses where long tasks fail rather than only whether they fail.
  Used for: per-attempt evidence capture and the long-horizon test
  suite (`daemon/tests/test_long_horizon.py`).
- [AMA-Bench: Evaluating Long-Horizon Memory for
  Agents](https://arxiv.org/abs/2602.22769) — Separates memory
  quality from task quality on long tasks. Used for: checkpoint and
  memory-surface design in runtime contracts.
- [The Horizon Gap: Progress and Failure in Long-Horizon
  Agents](https://arxiv.org/abs/2608.06663) — Tracks the widening gap
  between short-task and long-task competence. Used for: duration
  limits and timeout margins in the benchmark scheduler
  (`daemon/src/benchmarks/scheduler.py`).
- [METR Time Horizon 1.1](https://metr.org/blog/2026-1-29-time-horizon-1-1/)
  — Measures model capability as the task duration a model completes
  with fixed reliability. Used for: framing effort profiles and
  benchmark duration configuration.

## 4. Judge reliability and evaluation bias

- [Choice-Supportive Bias in Multi-Agent LLM
  Systems](https://ojs.aaai.org/index.php/AAAI/article/view/34843) —
  Agents defend earlier choices against contrary evidence. Used for:
  separated critic roles and the adjudication states in human review
  (`daemon/src/benchmarks/analysis.py` review states).
- [When Identity Skews
  Debate](https://aclanthology.org/2026.acl-long.650/) — Agent
  identity labels bias debate outcomes. Used for: neutral role
  naming in coordination prompts.
- [Controlling Uncertainty and Hallucination Risk in Multi-Agent
  Systems](https://proceedings.mlr.press/v337/kostka26a.html) —
  Correlated agent errors break independence assumptions. Used for:
  the case-level clustering doctrine in benchmark statistics: the
  case, not the attempt, is the statistical unit.
- [OAgents: An Empirical Study of Evaluation Variance in LLM
  Agents](https://aclanthology.org/2025.findings-emnlp.720/) — Agent
  scores vary heavily across identical reruns. Used for: repetitions
  as first-class outcome slots and seed recording per attempt.
- [Agent-as-a-Judge](https://proceedings.mlr.press/v267/zhuge25a.html)
  — Uses agentic judges with evidence trails instead of single-shot
  grading. Used for: the planned judge scorer class (work package
  3.3) and judge calibration (work package 3.4).
- [AI Agents That Matter](https://openreview.net/forum?id=Zy4uFzMviZ)
  — Argues cost-aware, reproducible agent evaluation with joint
  accuracy-cost reporting. Used for: cost as a first-class benchmark
  metric, the exact `Money` resource ledger, and cost-sensitive gates
  (`daemon/src/benchmarks/costs.py`).

## 5. Statistics for paired benchmark evaluation

Primary papers:

- [Resolution Diagnostics for Paired LLM Evaluation
  (arXiv:2605.30315)](https://arxiv.org/abs/2605.30315) — Frames
  paired evaluation as hypothesis testing and reports per-pair
  resolution against declared power targets; many public leaderboard
  gaps stay unresolved at conventional targets. Used for: case-level
  pairing, the practical-difference threshold, corrected significance
  in gates, and sample guidance
  (`daemon/src/benchmarks/analysis.py`, `docs/BENCHMARK_STATISTICS.md`).
- [Measuring Evaluation-Context Divergence in Open-Weight LLMs: A
  Paired-Prompt Protocol](https://arxiv.org/pdf/2605.06327) — Uses a
  paired-prompt protocol to detect context-sensitive behavior
  differences. Used for: the shared item-and-repetition seed across
  arms, so paired slots differ only in the declared treatment.
- [A General Framework for Design-Based Treatment Effect Estimation
  in Paired Cluster-Randomized
  Experiments](https://arxiv.org/pdf/2407.01765) — Design-based
  estimation when randomization pairs clusters. Used for: the
  family-stratified weighted case bootstrap with weights applied once
  in aggregation.
- [Why Pairing Your Bootstrap Is Necessary, And When It Stops
  Helping](https://dev.to/natnael_alemseged/why-pairing-your-bootstrap-is-necessary-and-when-it-stops-helping-2iim)
  — Practice note: paired resampling narrows intervals when task
  difficulty correlates across arms. Used for: the decision to
  resample cases, never independent arm samples.

Canonical methods the analysis engine implements:

- Wilson score interval (E. B. Wilson, 1927, *Probable Inference, the
  Law of Succession, and Statistical Inference*, JASA 22) — A binomial
  interval that stays defined at all-success and all-failure. Used
  for: labeled unclustered slot diagnostics
  (`wilson_interval` in `daemon/src/benchmarks/analysis.py`); a gate
  rule can never read one.
- McNemar's exact test (Q. McNemar, 1947, *Note on the sampling error
  of the difference between correlated proportions*, Psychometrika
  12) — An exact binomial test over discordant pairs. Used for:
  binary paired comparison, only after the predeclared binary case
  reduction (`mcnemar_exact`).
- Holm step-down correction (S. Holm, 1979, *A simple sequentially
  rejective multiple test procedure*, Scand. J. Statist. 6) —
  Family-wise error control without independence assumptions. Used
  for: corrected significance across every scorer comparison
  (`_holm_adjust`).
- BCa bootstrap intervals (B. Efron, 1987, *Better Bootstrap
  Confidence Intervals*, JASA 82) — Bias-corrected accelerated
  bootstrap intervals. Used for: per-arm descriptive metric intervals
  (`_mean_interval`).
- Sign-flip randomization tests (standard permutation inference; see
  E. Edgington and P. Onghena, *Randomization Tests*) — Exact or
  sampled inference by flipping paired signs under the null. Used
  for: the paired sign-flip test over the weighted case statistic,
  exact by enumeration for small case counts.
- SplitMix64 (G. Steele, D. Lea, C. Flood, 2014, *Fast Splittable
  Pseudorandom Number Generators*, OOPSLA) — A 64-bit mixing
  generator that any language reproduces exactly. Used for:
  `bmas-analysis-rng` with unbiased rejection sampling
  (`AnalysisRandom`), so bootstrap draws replay across
  implementations (`scripts/generate-statistical-oracle-fixtures.py`).

## 6. Scheduling and fair dispatch

- [Deficit round robin](https://en.wikipedia.org/wiki/Deficit_round_robin)
  — Weighted fair queueing through per-queue credit balances. Used
  for: the weighted round-robin turn allocation across runs
  (`claim_next_attempt` in `daemon/src/benchmarks/repository.py`).
- [Design a Distributed Job Scheduler
  (2026)](https://www.systemdesignhandbook.com/guides/design-a-distributed-job-scheduler/)
  — Practice guide: weighted fair share prevents noisy-tenant
  monopolies, and strict priority risks starvation. Used for: the
  three frozen priority bands, ticket-per-weight stride selection,
  and the explicit starvation-promotion events.
- [RSoC 2026: A new CPU scheduler for Redox
  (DWRR)](https://www.redox-os.org/news/rsoc-dwrr/) — A current
  deficit-weighted round-robin scheduler design and its latency
  tradeoffs. Used for: confirmation that band weights with bounded
  promotion is current practice.

## 7. Benchmark quality, reproducibility, and outcome taxonomies

- [Benchmarking the Benchmarks: A Validity Audit of Tool-Calling
  Evaluation](https://arxiv.org/pdf/2607.02577) — Builds a unified
  taxonomy of evaluation failures: exact-match constraints, state
  over-specification, annotation errors, rubric drift, and judge
  variance. Used for: the `OutcomeMapping` contract that pins every
  terminal reason to a benchmark class, retry rule, missingness rule,
  and denominator rule (`daemon/src/benchmarks/outcome_mappings.py`).
- [ErrorMap and ErrorAtlas: Charting the Failure Landscape of Large
  Language Models](https://arxiv.org/pdf/2601.15812) — Charts failure
  categories at scale and shows category drift across versions. Used
  for: versioned, digested outcome mappings, where a changed mapping
  needs a new mapping set and run plan.
- [Large Language Model Benchmarks: A Taxonomy of Capabilities,
  Scientific Quality Assessment, and Saturation Analysis
  (BQAI)](https://doi.org/10.3390/make8060141) — A weighted composite
  index for benchmark quality across annotation, standardization,
  reproducibility, robustness, coverage, and fairness. Used for: the
  Phase 0 correctness-repair checklist framing in
  `docs/plans/02-benchmarking/`.
- [LLMEval-Fair: A Large-Scale Longitudinal Study on Robust and Fair
  Evaluation of Large Language Models](https://arxiv.org/pdf/2508.05452)
  — Longitudinal evidence that unpinned configurations and drifting
  scorers corrupt comparisons. Used for: immutable test revisions,
  scorer configuration checksums on every stored score, and the
  invariant digest.
- [Evaluating LLM Systems: Metrics and Benchmarks
  (2026)](https://futureagi.com/blog/evaluating-llm-systems-metrics-benchmarks-2026/)
  — Practice checklist: a healthy benchmark has reproducible reruns,
  explainable failures, and score movement that matches trace
  signals. Used for: the gate terminality and status-separation
  repairs (work packages 0.1 and 0.2).

## 8. Evaluation platform practice

- [Demystifying evals for AI
  agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
  — The anatomy of agent evaluations: tasks, graders, transcripts,
  and iteration. Used for: the benchmark domain model
  (`docs/benchmark-domain.md`).
- [Tau-bench](https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b126cc38b8638e07bef37e7b2bb72bf-Abstract-Conference.html)
  — Tool-agent-user benchmark with pass^k reliability over repeated
  trials. Used for: repetition slots and binary case reductions
  (strict-majority, all, at-least-k).
- [PaperBench](https://openai.com/index/paperbench/) — Grades long
  replication tasks through rubric trees with evidence. Used for:
  the planned rubric scorer class and evidence bundles.
- [Inspect evaluation logs](https://inspect.aisi.org.uk/eval-logs.html)
  — Structured, replayable evaluation logs as the artifact of record.
  Used for: durable attempt evidence, execution snapshots, and the
  analysis-replay bundle plan (work package 4.7).
- [Harbor dataset adapters](https://www.harborframework.com/docs/datasets/adapters)
  — Adapter registry pattern for dataset import. Used for: the
  planned source adapter registry (work package 2.1).
- [Hugging Face Dataset Viewer quick start](https://huggingface.co/docs/dataset-viewer/en/quick_start)
  and [Parquet API](https://huggingface.co/docs/dataset-viewer/en/parquet)
  — Official APIs for revision-pinned dataset access. Used for: the
  planned Hugging Face import (work package 2.2) with exact revision
  pinning.

## 9. Standards

- [RFC 8785, JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
  — Deterministic JSON bytes for hashing and signing. Used for: the
  Foundation digest profile and every content checksum
  (`daemon/src/core/digest_profile.py`), including the
  `bmas/outcome-mapping-set` digest.
- [RFC 6901, JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)
  and [RFC 6902, JSON Patch](https://www.rfc-editor.org/rfc/rfc6902)
  — Addressing and mutation of JSON documents. Used for: the
  PatchBoard mutation contract.
- [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12)
  — Configuration validation. Used for: runtime configuration
  schemas, scorer configuration schemas, and the generated
  `bmas.yaml` schema (`docs/reference/config.schema.json`).
- [SQLite write-ahead logging](https://sqlite.org/wal.html) —
  Concurrent reader-writer durability. Used for: the daemon database
  configuration (`daemon/src/database.py`).
- [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
  and [RFC 9110, HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110)
  — Outbound request safety and redirect-credential rules. Used for:
  the URL guard (`daemon/src/core/url_guard.py`).
- [WASI](https://wasi.dev/) and
  [Wasmtime deterministic execution](https://docs.wasmtime.dev/examples-deterministic-wasm-execution.html)
  — Capability-scoped, deterministic sandboxing. Used for: the
  planned scorer plugin sandbox (work package 3.3).

## Update rule

Add one entry when a new source shapes a design decision or an
implementation. Name the exact module, plan, or document that uses
it. Remove an entry only when the code and the plans no longer use
the source.
