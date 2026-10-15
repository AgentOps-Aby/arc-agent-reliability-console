# ARC metrics spec

Computed by `analysis/compute_metrics.py` on `data/arc.db` → `data/metrics_snapshot.json`.
Finished tasks only (`still_running = 0 AND ended_cancelled = 0`). Success = `completed` + (`passed` or `skipped`).
Category metrics use `runs.failure_category` (classifier, Step 6), falling back to `ground_truth.true_failure_category` until then.

## North star: Task Success Rate (TSR), per agent vs `agent_targets`

TSR = `AVG(eventual_success)` over finished tasks. Fleet: **92.7%**. Never shown without its guardrails:
First-Attempt Success Rate (retry masking), Cost per Successful Task (buying success), check coverage + unverified share
(skipping checks). Verified TSR was considered and rejected as the north star: on this data it mostly measures check coverage.

| # | Metric | Formula (Step 2 fields) | Action it drives | Healthy | Alarming | Pitfall |
| --- | --- | --- | --- | --- | --- | --- |
| M1 | First-Attempt Success Rate | `AVG(first_attempt_success)` per task | Large retry lift → fix root cause, not retries | lift (TSR − FASR) ≤ 5 pts | lift > 10 pts; FASR −5 pts after deploy | Retries keep TSR flat while FASR falls |
| M2 | Task Success Rate | `AVG(eventual_success)` per task | Below target → drill into M3 | ≥ agent target | below target 2+ days | Unverified successes inflate it |
| M3 | Failure mix | failed runs per (agent, category) / failed runs per agent | Work the biggest category; route to owner | no category > 40%; unknown < 10% | a share doubles vs 7-day baseline; unknown > 10% | Shares hide volume; taxonomy changes shift mix |
| M4 | Retry Recovery Rate | per first-failure category: `AVG(eventual_success)` of retried tasks | < 30% → stop auto-retrying that category | ≥ 70% for retried categories | any auto-retried category < 30% | Only retried tasks count; filed under first failure |
| M5 | Wasted Retry Cost | `SUM(cost_usd)` of runs with `retry_count > 0` in tasks with `eventual_success = 0` | First place to cut spend | < 25% of retry spend | > 50% of retry spend or > 10% of total | Failed retry before a successful one = recovery cost, not waste |
| M6 | Cost per Successful Task | `SUM(total_cost_usd) / SUM(eventual_success)` | Over cap → check M5, M8, token growth | ≤ cap, flat | > cap or +20% WoW | Spikes when successes drop; hides task-type mix |
| M7 | p50 / p95 latency | run: `ended_at − started_at`; task: `MAX(ended_at) − first_started_at`; tool: `steps.latency_ms` | Task p95 ≫ run p95 → retries are the delay; tool p95 near timeout → fix tool | run p95 ≤ 2× p50; tool p95 < 50% of timeout | p95 +50% after deploy; tool p95 > 80% of timeout | Never chart means; completed-only hides fast failures |
| M8 | Runs hitting max steps | `COUNT(step_count >= 25) / COUNT(*)`; cost share likewise | Loop detection, stop conditions | < 1% of runs, < 5% of spend | > 3% of runs or > 10% of spend | Only catches loops that reach the limit |
| S1 | Error budget used | `(1 − TSR) / ((100 − target) / 100)` | > 100% → freeze risky deploys | < 80% | > 100% | 30-day window hides a fresh burn |
| S2 | Check coverage / unverified share | `AVG(is_checked)`; `SUM(eventual_success − eventual_verified_success) / SUM(eventual_success)` | Low → add output checks before trusting TSR | ≥ 80% / < 15% | < 60% / > 30% | Weak checks raise coverage without value |

## Results (seed 42, Sep 2026)

| Agent | FASR | TSR | Target | Budget used | $/success | Cap | Run p50/p95 | Task p95 | Max-steps rate | Coverage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| support-triage | 92.5% | 95.9% | 92% | 52% | $0.037 | $0.045 | 9 / 15 s | 17 s | 0.4% | 71.1% |
| code-fix | 78.5% | 85.1% | 83% | 88% | **$1.260** | $1.00 | 43 / 88 s | 149 s | **5.9%** | 94.6% |
| web-research | 81.4% | 92.1% | 93% | **113%** | $0.409 | $0.45 | 23 / 38 s | 114 s | 2.6% | **56.9%** |
| invoice-entry | 89.5% | 94.7% | 97% | **176%** | $0.034 | $0.040 | 9 / 16 s | 24 s | 0.4% | 92.5% |
| fleet | 86.5% | 92.7% | | | $0.335 | | | | | |

Retry recovery (ground truth categories): output_parse_error 94.1%, tool_error 89.4%, rate_limit 86.9%, timeout 63.7%,
unknown 56.5%, loop_max_steps 30.2%, context_overflow 13.9%, silent_wrong_output 11.1%, invalid_tool_args 5.5%.
Wasted retry cost: **$214.11 of $303.54 retry spend (70.5%)**, 15.8% of all spend. `web.fetch` p95: 3.9 s (v17) → 27.5 s (v18) → 4.0 s (v19).
