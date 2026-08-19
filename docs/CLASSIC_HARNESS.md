# Classic Blackboard Harness

The classic harness verifies the complete blackboard lifecycle with deterministic workers. It does not test patchboard or stigmergic coordination.

## Fast lifecycle suites

Run the complete daemon harness from the `daemon` directory.

```bash
../.venv/bin/python -m pytest tests/ -q
```

The suites cover these areas.

- `test_classic_golden_lifecycle.py` runs every fixed role and generated expert.
- `test_classic_restart_delivery.py` tests commit loss, replay, duplicate delivery, and partial checkpoints.
- `test_classic_provider_faults.py` tests network faults, invalid responses, cancellation, fallback, and endpoint circuits.
- `test_classic_coordination_matrix.py` tests invalid selections, disabled roles, stalls, conflicts, and minority correction.
- `test_classic_long_board.py` tests 503 board entries, bounded context, memory use, and retrieval recall.
- `classic_harness.py` checks event, activation, replay, provenance, privacy, cost, and idempotency rules after each round.

The golden lifecycle runs in sequential and concurrent modes. Both modes must produce the same result and board state.

## Soak runner

The evaluation harness provides action horizons of 1, 5, 10, 25, 50, and 100. It requires at least ten repetitions for each fixed configuration.

Supply an asynchronous driver that runs one `TrialSpec`. Return a `TrialOutcome` with the observed values.

```python
from eval.soak import SoakHarness, TrialOutcome


async def run_trial(spec):
    result = await run_classic_workload(spec)
    return TrialOutcome(
        effective_actions=result.actions,
        exact_success=result.exact_success,
        completed=result.completed,
    )


report = await SoakHarness(repetitions=10).run(
    {"classic-concurrent": {"round_execution": "concurrent"}},
    run_trial,
)
report.save("eval/results/classic-soak.json")
```

The runner gives each trial a stable random seed. A failed driver call creates a failed trial record and does not stop the run.

The report includes these measurements.

- Exact task success and strict repeated-run success.
- False completion rate and reliability decay by horizon.
- Restart recovery rate and duplicate external actions.
- Budget overshoot and context retrieval recall.
- Stall, replan, and unresolved conflict counts.
- Cost and latency by role.
- Minority correction rate.

The driver must use each external idempotency key as the corresponding `external_action_keys` value. Duplicate values count as duplicate actions.
