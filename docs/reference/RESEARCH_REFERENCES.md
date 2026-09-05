# Research References

This document is the organized index of every external paper,
standard, and practice source that bmas design and code build on.
Each entry gives a short description and names the exact place where
bmas uses it. The plan-time audit narratives live in the local
planning workspace at `docs/plans/RESEARCH_RECORD.md`, which stays
outside version control. This index stays current: add one entry in
the matching category when a new source shapes a design or an
implementation.

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

- [Why Do Multi-Agent LLM Systems Fail? (MAST, 2026
  update)](https://arxiv.org/abs/2503.13657) and [When Errors Become
  Narratives: A Longitudinal Taxonomy of Silent Failures in a
  Production LLM Agent Runtime](https://arxiv.org/pdf/2606.14589) —
  MAST names fourteen failure modes across specification and design,
  inter-agent misalignment, and task verification with expert kappa
  of 0.88; the longitudinal study shows silent failures need explicit
  classes in the trace. Used for: the multi-agent families and their
  classes in `daemon/src/benchmarks/failure_taxonomy.py`.

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

- [How to Calibrate Your LLM Judge With Human
  Annotations](https://galileo.ai/blog/calibrate-llm-judge-human-annotations)
  and [LLM-as-a-Judge in 2026: How It Works, When It
  Fails](https://futureagi.com/blog/llm-as-a-judge/) — Current
  practice: validate every judge against a representative
  human-labeled sample, report Cohen's kappa for two raters and
  Krippendorff's alpha for more, and treat a kappa below 0.4 as an
  ambiguous rubric. Used for: the calibration record with raw
  agreement, kappa only when defined, and the agreement threshold in
  `daemon/src/benchmarks/judge_calibration.py`.
- [Reliability without Validity: A Systematic, Large-Scale Evaluation
  of LLM-as-a-Judge Models Across Agreement, Consistency, and
  Bias](https://arxiv.org/html/2606.19544v1) and [The Coin Flip
  Judge? Reliability and Bias in LLM-as-a-Judge
  Evaluation](https://arxiv.org/pdf/2606.13685) — Large-scale 2026
  evidence that cross-judge agreement sits near kappa 0.5 on
  subjective tasks and that self-preference bias appears when a judge
  shares a candidate's model. Used for: the recorded judge
  independence check, the drift policy between judge versions, and
  the visible abstention and invalid-output rates.

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

- [Bootstrap Confidence Intervals for LLM Evaluation (Indeed
  Engineering, 2026)](https://engineering.indeedblog.com/blog/2026/07/bootstrap-confidence-intervals-for-llm-evaluation/)
  and [When +1% Is Not Enough: A Paired Bootstrap Protocol for
  Evaluating Small Improvements](https://arxiv.org/html/2511.19794v1)
  — Multiple runs per input form clustered data; a paired cluster
  bootstrap resamples items, never nested runs, and intervals that
  treat clustered samples as independent come out too narrow. Used
  for: the family-stratified weighted case bootstrap with cases as
  the only resampling unit in
  `daemon/src/benchmarks/frozen_analysis.py`.
- [Resolution Diagnostics for Paired LLM
  Evaluation](https://arxiv.org/pdf/2605.30315) — Paired designs
  need explicit diagnostics for whether the sample can resolve the
  declared difference. Used for: the predeclared minimum usable case
  count and the small-cluster insufficiency rule in the frozen
  comparison gates.
- [FDA guidance: Non-Inferiority Clinical Trials to Establish
  Effectiveness](https://www.fda.gov/media/78504/download) and
  [Multiplicity and multiple-endpoint testing
  guide](https://meddeviceguide.com/blog/multiplicity-multiple-endpoints-medical-device-clinical-trials-guide)
  — The margin is pre-specified, non-inferiority tests before
  superiority, and a Holm procedure controls the family. Used for:
  the predeclared non-inferiority margin, direction, hypothesis
  order, and Holm correction inside one declared comparison family.

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

- [FinOps for AI in 2026: why traditional FinOps breaks on AI
  workloads](https://leanopstech.com/blog/finops-for-ai-2026/) and
  [Bringing FinOps to Your LLMs: understanding and tracking OpenAI
  spend](https://www.finout.io/blog/track-openai-spend) — Every
  provider bills differently, actual invoices arrive late, and the
  FOCUS specification normalizes billing into one schema with
  separate estimate and invoice datasets. Used for: the resource
  ledger entry shape with separate estimate and actual objects,
  provider text kept as evidence, pricing versions, and reconciliation
  versions in `daemon/src/benchmarks/resource_ledger.py`.

- [Synthetic Users, Real Differences: an Evaluation Framework for
  User Simulation in Multi-Turn
  Conversations](https://arxiv.org/pdf/2605.02624) and [VISTA: A
  Versatile Interactive User Simulation Toolkit for Agent
  Evaluation](https://arxiv.org/pdf/2606.11079) — Multi-turn agent
  evaluation needs simulated users with pinned behavior and explicit
  turn control, and different simulator behaviors lead agents down
  different paths. Used for: the registered simulator versions with
  pinned prompt, model, image, dependency, and random-schedule digests
  in `daemon/src/benchmarks/interaction_execution.py`.
- [Multi-Turn LLM Evaluation in 2026 (Confident
  AI)](https://www.confident-ai.com/blog/multi-turn-llm-evaluation-in-2026)
  — Practice guide for bounded multi-turn evaluation with turn
  limits, scenario stop conditions, and per-turn verdicts. Used for:
  the turn, action, token, time, and cost limits and the declared
  stop conditions of the interaction executor.
- [ISSTA 2026 artifact
  evaluation](https://conf.researchr.org/track/issta-2026/issta-2026-artifact-evaluation)
  and [A Reproducibility Protocol for Cross-Implementation Evaluation
  of Post-Quantum ACVP Test
  Vectors](https://arxiv.org/html/2608.13784v1) — Release artifacts
  carry machine-readable manifests with per-member digests, frozen
  toolchain summaries, file allowlists, and prohibited-pattern scans
  so reviewers detect drift. Used for: the replay bundle member
  manifest, digest verification before publication, quarantine of
  executable members, and the toolchain member in
  `daemon/src/benchmarks/replay_bundle.py`.

- [Playwright Global Setup and Teardown: Complete 2026
  Guide](https://qaskills.sh/blog/playwright-global-setup-teardown-guide)
  and [Playwright Testing Best Practices for
  2026](https://qaskills.sh/blog/playwright-testing-best-practices-2026)
  — Run-wide setup seeds deterministic state and tears it down the
  same way, fixtures own their data, and web-first assertions replace
  hard waits. Used for: the test-stack controller in global setup and
  teardown and the unmocked journey in
  `mission-control/e2e/full-stack/`.
- [Playwright Flaky Tests: 2026 Diagnostic
  Playbook](https://testquality.com/playwright-flaky-tests-diagnostic-playbook-2026/)
  — A retry that passes hides a real first failure unless the run
  keeps its artifacts and reports the test as flaky. Used for: the
  flake reporter, the preserved first-attempt artifacts, and the zero
  flake budget in `mission-control/playwright.config.ts`.

## 9. Data contracts and schema evolution

- [Expand and Contract: A Pattern to Apply Breaking Changes to
  Persistent Data with Zero
  Downtime](https://www.tim-wellhausen.de/papers/ExpandAndContract/ExpandAndContract.html)
  — The canonical pattern paper: expand additively first, backfill,
  and contract only after every consumer moved. Used for: the
  evaluation storage expansion (migration adds tables beside V1,
  deletes and renames nothing) and the phased plan in
  `daemon/src/benchmarks/evaluation_records.py`.
- [Schema changes and the power of expand-contract
  (pgroll)](https://xata.io/blog/pgroll-expand-contract) — A current
  implementation of multi-phase schema evolution where both
  generations stay readable during the transition. Used for: keeping
  V1 records readable while the V2 tables exist, and the deletion
  gates before any contract phase.
- [Zero-Downtime Database Migrations: Expand/Contract, Triggers, and
  Shadow Reads](https://thebackenddevelopers.substack.com/p/zero-downtime-database-migrations)
  — Practice guide: additive changes, trigger-enforced invariants,
  and verified reads before cutover. Used for: the immutable
  publication triggers on every published or historical evaluation
  record.
- [Data Contracts in Practice: Schema Versioning, Evolution, and
  Producer-Consumer
  Agreements](https://www.datasops.com/blog/data-contracts-versioning)
  — A published schema version is immutable; a change creates the
  next version. Used for: the published schema files under
  `docs/reference/evaluation-contracts/` and the contract generation
  in record metadata.
- [Schema Registry Data Contracts
  (Confluent)](https://docs.confluent.io/platform/current/schema-registry/fundamentals/data-contracts.html)
  — A registry as the single source of truth with validation and
  declared migration rules. Used for: one definitions module as the
  schema authority, with generated published files and a freshness
  check (`scripts/generate-evaluation-contract-schemas.py`).
- [Data Contracts: Implementation Guide with Schema and CI/CD
  Examples (2026)](https://datadef.io/guides/en/data-contracts) —
  Contracts are enforceable only when the pipeline validates them.
  Used for: contract validation at the write boundary and in the
  registered manifest group `daemon.evaluation-contracts`.
- [Expand and Contract: the strangler fig
  migration](https://oneuptime.com/blog/post/2026-01-24-strangler-fig-migration-pattern/view)
  and the [pattern
  overview](https://firstprinciplesengineering.tech/01-fundamentals/01-concepts/02-architecture/05-strangler-fig)
  — One facade routes both generations while legacy and current
  implementations co-exist, with a rollback path at every step. Used
  for: the version-aware evaluation facade
  (`daemon/src/benchmarks/facade.py`) and the phased migration with
  declared rollbacks (`daemon/src/benchmarks/evaluation_migration.py`).
- [Data strangulation playbook: backfill, read cutover, write
  cutover](https://www.catio.tech/blog/strangler-fig-pattern) — The
  data half of the migration proceeds per domain with validation
  before each irreversible step. Used for: the idempotent backfill
  with digest checks, the dual-read fallback evidence, and the
  measured deletion gates before the contract phase.

- [The Strangler Fig Pattern: How to Modernize Legacy Systems Without
  a Big Bang Rewrite (Security Boulevard,
  2026)](https://securityboulevard.com/2026/07/the-strangler-fig-pattern-how-to-modernize-legacy-systems-without-a-big-bang-rewrite/)
  — A slice retires only after concrete exit criteria hold: no live
  traffic on the legacy route, no remaining readers, historical data
  handled, monitoring that confirms zero usage, and a closed rollback
  window. Used for: the measured fallback window with its declared
  threshold, the populated rollback evidence, and the retention
  evidence every removal gate needs in
  `daemon/src/benchmarks/evaluation_migration.py`.

## 10. Dataset lineage and deterministic transformation

- [Data Lineage Tools in 2026: Where Lineage
  Lives](https://datahub.com/blog/data-lineage-tools/) — Surveys
  current lineage practice: lineage lives with the data platform,
  captures column-level derivation, and stays queryable at decision
  time. Used for: the lineage carried by every publication (sources,
  parent version, recipe digest, and content digest) in
  `daemon/src/benchmarks/draft_editor.py`.
- [Data Lineage for Machine Learning: Why It
  Matters](https://datahub.com/blog/data-lineage-for-ml/) — Argues
  that ML datasets need end-to-end lineage from raw source to
  training or evaluation artifact, because a model inherits every
  upstream defect. Used for: the publish confirmation view and the
  frozen version lineage in the draft editor, and the rule that trust
  restrictions derive through that lineage.
- [A Deterministic Forensic Preprocessing Framework for Heterogeneous
  Network Datasets](https://arxiv.org/abs/2606.11565) — Formalizes
  preprocessing as deterministic, order-stable transformations whose
  outputs verify by content hash across independent runs. Used for:
  the `bmas-transform` profile design (pinned rules, stable ordering,
  and digest verification on rebuild) in
  `daemon/src/benchmarks/transform_profile.py`.
- [Koji: Automating pipelines with mixed-semantics data
  sources](https://arxiv.org/abs/1901.01908) — Uses causal hashing:
  a target's hash derives from the hashes of its inputs and its
  transformation, so equal inputs and recipes give equal outputs.
  Used for: the dataset digest over ordered case digests and the
  recipe digest binding in `apply_recipe`, and the publish-time
  rebuild check that blocks on a digest mismatch.

## 11. Standards

- [RFC 8785, JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
  — Deterministic JSON bytes for hashing and signing. Used for: the
  Foundation digest profile and every content checksum
  (`daemon/src/core/digest_profile.py`), including the
  `bmas/outcome-mapping-set` digest, and the portable
  `bmas-transform` canonicalization with ECMAScript number rendering
  (`daemon/src/benchmarks/transform_profile.py`).
- [RFC 6901, JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)
  and [RFC 6902, JSON Patch](https://www.rfc-editor.org/rfc/rfc6902)
  — Addressing and mutation of JSON documents. Used for: the
  PatchBoard mutation contract and the template binding pointers in
  the `bmas-transform` grammar.
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
  the URL guard (`daemon/src/core/url_guard.py`) and the egress
  broker (`daemon/src/benchmarks/import_worker.py`).
- [SSRF protection that resolves DNS: pinning and
  redirects](https://lyrashieldai.com/blog/ssrf-protection-dns-redirects)
  and the [SSRF practitioner guide
  (2026)](https://techearl.com/server-side-request-forgery) — Current
  practice: resolve once, validate every answer, dial the exact
  pinned address with the Host header and TLS hostname preserved, and
  reapply the whole policy on every redirect. Used for: the pinned
  connections, peer revalidation, and per-hop revalidation in the
  egress broker.
- [DNS rebinding against SSRF
  filters](https://aydinnyunus.github.io/2026/03/14/ssrf-dns-rebinding-vulnerability/)
  — Shows the validation-to-connection race that a short-TTL record
  exploits. Used for: the design decision that the transport never
  resolves again after validation.
- [WASI](https://wasi.dev/) and
  [Wasmtime deterministic execution](https://docs.wasmtime.dev/examples-deterministic-wasm-execution.html)
  — Capability-scoped, deterministic sandboxing: fuel interruption is
  fully deterministic, NaN canonicalization gives one canonical NaN,
  and deterministic execution requires virtualized clocks and
  filesystems. Used for: the scorer sandbox boundary contract
  (`daemon/src/benchmarks/scorer_sandbox.py`) with its fuel
  accounting, NaN canonicalization, disabled relaxed SIMD, and
  logical-time and deterministic-random interfaces.
- [Wasmtime security](https://docs.wasmtime.dev/security.html) and
  [safe module termination with epoch
  interruption](https://www.systemshardening.com/articles/wasm/wasmtime-epoch-interruption-security/)
  — Epoch or deadline interruption is a safety mechanism, not a
  deterministic limit. Used for: the rule that a host deadline is
  only a last-resort kill, records `sandbox_wall_time_kill`, and
  never enters a byte-identical replay claim.
- [The state of microVM isolation in
  2026](https://emirb.github.io/blog/microvm-2026/) and
  [sandboxing AI agents in 2026
  (Northflank)](https://northflank.com/blog/how-to-sandbox-ai-agents)
  — Current consensus: a normal container is not a sandbox for
  untrusted code, and microVMs with their own guest kernel are the
  production-safe isolation layer. Used for: the
  `NativeScorerSandboxSpec` requirement that approved native scorers
  run in a pinned microVM and that a container is never the only
  isolation boundary.
- [From Agent Traces to Trust: A Survey of Evidence Tracing and
  Execution Provenance in LLM
  Agents](https://arxiv.org/abs/2606.04990) — Surveys agent trace
  artifacts and maps them onto provenance models such as W3C
  PROV-DM: instructions, tool calls, observations, claims, and final
  responses all need recorded derivation. Used for: the complete
  attempt evidence bundle sections in
  `daemon/src/benchmarks/evidence_capture.py`.
- [Evidence-Ledger Adjudication for Claim-Evidence
  Traceability](https://arxiv.org/html/2607.26512v1) — Argues for an
  explicit ledger connecting every claim to its supporting evidence
  before any adjudication. Used for: the claims-and-verification
  section of the evidence bundle and the rule that every stored
  score references immutable evidence through enforced links.

### Vectorized analysis, real sandboxes, and data-class redaction

- [NumPy bit generators and parallel random
  numbers](https://numpy.org/doc/stable/reference/random/bit_generators/index.html)
  and [SciPy `bootstrap` with vectorized
  statistics](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html)
  — The current practice derives every replicate from its own
  counter-addressed stream so output stays invariant to batch order,
  parallelism, and thread count, and processes resamples in batches
  with memory bounded by batch size times sample size. Used for: the
  keyed-counter derivation of `bmas-analysis-rng` algorithm version
  2 in `daemon/src/benchmarks/analysis_rng.py`, and the batched,
  threaded, bit-identical engine in
  `daemon/src/benchmarks/analysis_engine.py`.
- [Bootstrap confidence intervals for LLM evaluation (Indeed
  Engineering, July
  2026)](https://engineering.indeedblog.com/blog/2026/07/bootstrap-confidence-intervals-for-llm-evaluation/)
  — Recommends the paired cluster bootstrap with percentile intervals
  when comparing two systems, carrying every repetition of a chosen
  input into the resample. Used for: the frozen gate rules in
  `daemon/src/benchmarks/gates.py`, which decide a baseline gate
  from the paired frozen comparison of one arm across two runs.
- [Wasmtime-py API
  documentation](https://bytecodealliance.github.io/wasmtime-py/) and
  [WASIp2 in Wasmtime](https://docs.wasmtime.dev/examples-wasip2.html)
  — The Python bindings expose fuel, store limits, epoch
  interruption, NaN canonicalization, and the component model
  linker; a component that imports an interface the linker never
  defines fails before instantiation. Used for:
  `daemon/src/benchmarks/sandbox_backends.py`, which runs scorer
  components with fuel as the deterministic limit, the store limiter
  for memory and tables, and epoch interruption as the last-resort
  kill.
- [Firecracker jailer and snapshot
  system](https://github.com/firecracker-microvm/firecracker/blob/main/CHANGELOG.md)
  and [The Firecracker jailer
  explained](https://www.pandastack.ai/blog/firecracker-jailer-explained/)
  — The jailer applies the chroot, the namespaces, the cgroup
  limits, and the seccomp filter before it executes the virtual
  machine monitor, and the API is one REST surface over a Unix
  socket with vsock as the host-to-guest channel. Used for: the
  Firecracker runner in `daemon/src/benchmarks/sandbox_backends.py`,
  which verifies the kernel, root filesystem, and monitor digests,
  configures no network device, and authenticates the vsock request
  channel.
- [Data classification: technical implementation
  guide](https://talkthinkdo.com/guides/development-practice/data-classification-implementation/)
  and [data classification and labeling in
  2026](https://concentric.ai/the-importance-of-data-classification-levels-and-labels/)
  — Classification should drive redaction, export, and erasure
  through declared labels, so a newly labeled field comes under
  redaction automatically. Used for:
  `daemon/src/benchmarks/data_classes.py`, the declarative policy
  with named field classes, measurement markers, value detectors,
  and one published policy digest that every envelope, evidence
  bundle, ledger entry, and export pins.
- [LLM-as-judge best practices in 2026: calibration, bias, and
  cost](https://futureagi.com/blog/llm-as-judge-best-practices-2026/)
  and [Who drifted: the system or the judge?](https://arxiv.org/html/2606.15474)
  — Judges drift within weeks; a fixed anchor set, a calibration job
  against it, and a drift monitor on agreement are the minimum. Used
  for: the anchor sets and the weekly calibration schedule in
  `daemon/src/benchmarks/judge_calibration.py` and the model-backed
  judge in `daemon/src/benchmarks/model_backed.py`.

### Frozen evaluation screens

- [Introduction to forest plots](https://cran.r-project.org/web/packages/forestplot/vignettes/forestplot.html)
  and [Non-inferiority trials: understanding the
  concepts](https://s4be.cochrane.org/blog/2022/03/18/understanding-non-inferiority-trials/)
  — A non-inferiority decision reads as one forest-plot row: the
  interval, its point estimate, the zero line, and the predeclared
  margin on one shared axis, so the reader sees whether the interval
  clears the margin. Used for: `FrozenDecisionBar` and the layout
  helpers in `mission-control/src/lib/frozen-report-presentation.ts`.
- [Wizard and stepper pattern (UX patterns for
  developers)](https://uxpatterns.dev/patterns/advanced/wizard) and
  [Beyond the progress bar: the art of stepper UI design
  (2026)](https://lollypop.design/blog/2026/february/beyond-the-progress-bar-the-art-of-stepper-ui-design/)
  — A lifecycle stepper needs semantic list markup, `aria-current`
  on the active step, and one clear next action per state. Used for:
  the metric definition lifecycle screen in
  `mission-control/src/app/metrics/[metricId]/MetricDetailClient.tsx`
  and `mission-control/src/lib/metric-lifecycle-presentation.ts`.
- [Dual view design pattern
  (Microsoft)](https://learn.microsoft.com/dual-screen/design/dual-view)
  — Two versions of the same content compare best side by side with
  every changed value marked. Used for: the analysis history panel
  and `mission-control/src/lib/analysis-history-presentation.ts`.

### Evaluation operations screens

- [LLM-as-Judge best practices in 2026: calibration, bias, and
  cost](https://futureagi.com/blog/llm-as-judge-best-practices-2026/)
  and [How to calibrate your LLM judge with human
  annotations](https://galileo.ai/blog/calibrate-llm-judge-human-annotations)
  — A judge calibrates against a human-labelled anchor set, reports
  Cohen's kappa beside raw agreement, recalibrates on a schedule and on
  every judge version change, and a drift monitor alerts when the
  agreement drops beyond a tolerance. Used for: the judge calibration
  screen in `mission-control/src/app/judges/JudgesPageClient.tsx` and
  `mission-control/src/lib/judge-calibration-presentation.ts`.
- [Complete guide to reconciliation dashboards
  (2026)](https://www.osfin.ai/blog/reconciliation-dashboard) and
  [When your settlement doesn't match: a practical troubleshooting
  guide](https://reconcileos.com/blog/when-settlement-doesnt-match-practical-troubleshooting-guide)
  — A reconciliation view keeps every unmatched or unpriced item as a
  first-class row with its reason, aligns each settlement version with
  the version it supersedes, and never overwrites an earlier version.
  Used for: the resource ledger panel in
  `mission-control/src/components/features/ResourceLedgerPanel.tsx`,
  `mission-control/src/lib/resource-ledger-presentation.ts`, and the
  supersession link in `daemon/src/benchmarks/resource_ledger.py`.
- [Pre-registration: why it
  matters](https://metricgate.com/blogs/pre-registration-why-it-matters/)
  and [Pre-analysis plans
  (J-PAL)](https://www.povertyactionlab.org/resource/pre-analysis-plans)
  — A study commits to its hypotheses, sample size, analysis plan, and
  exclusion rules before any data collects, and the committed plan
  cannot change silently. Used for: the study authoring screen with its
  preview and publication in
  `mission-control/src/app/studies/StudiesPageClient.tsx`,
  `mission-control/src/lib/study-presentation.ts`, and the admission
  verdict read route in `daemon/src/routes/evaluation.py`.

## Update rule

Add one entry when a new source shapes a design decision or an
implementation. Name the exact module, plan, or document that uses
it. Remove an entry only when the code and the plans no longer use
the source.
