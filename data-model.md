# ARC data model

ARC stores **one row per attempt** in `runs` and **one row per LLM call, tool call or output check** in `steps`.
Retries are new runs that share a `task_id` and point to the run they replaced through `parent_run_id`.

- **Task**: one unit of work an agent was asked to do. Identified by `task_id` (not a separate table in v1).
- **Run**: one attempt at a task. First attempt has `retry_count = 0`.
- **Step**: one LLM call, tool call or output check inside a run.

```mermaid
erDiagram
    RUNS ||--o{ RUNS : "retried by (parent_run_id)"
    RUNS ||--o{ STEPS : "has (run_id)"
    RUNS {
        text run_id PK
        text task_id "groups attempts"
        text parent_run_id FK "run it retried"
        int retry_count
        text retry_trigger
        text agent_name
        text task_type
        text deploy_version
        text model_name
        text model_version
        text started_at
        text ended_at
        text status "did it finish"
        text outcome_check "did output pass"
        text failure_category "one of 10 taxonomy codes"
        text taxonomy_version "rule set that labeled it"
        text final_error_message
        int step_count
        int input_tokens
        int output_tokens
        decimal cost_usd
    }
    STEPS {
        text step_id PK
        text run_id FK
        int step_index
        text step_type "llm_call | tool_call | output_check"
        text tool_name
        text started_at
        int latency_ms
        text status "ok | error | timeout"
        text error_type
        text error_message
        int input_tokens
        int output_tokens
        decimal cost_usd
    }
```

## Success rule (updated in Step 3)

| status | outcome_check | Result |
| --- | --- | --- |
| completed | passed | Success (verified) |
| completed | skipped | Success (unverified), reported as a share next to TSR with check coverage |
| completed | failed | Failure: `silent_wrong_output` |
| cancelled | any | Neither: excluded from success-rate metrics (updated in Step 4) |
| anything else | any | Failure, classified per [failure-taxonomy.md](failure-taxonomy.md) |

## taxonomy_version

Which version of `classifier/taxonomy.json` set `failure_category`. If a category's share jumps, this tells you whether the
failures changed or the rules did.

## agent_targets

One row per agent, like an uptime SLO per service: `target_task_success_pct`, `max_cost_per_successful_task_usd`,
`error_budget_window_days` (30), `effective_from`. Error budget used = actual failure share / allowed failure share.
Seed values: [`schema/seed_agent_targets.sql`](../schema/seed_agent_targets.sql) for `support-triage`, `code-fix`,
`web-research`, `invoice-entry`.

## Core metrics (per task)

| Metric | Definition |
| --- | --- |
| First-Attempt Success Rate | Tasks whose first run succeeded / finished tasks |
| Task Success Rate (TSR) | Tasks where any run succeeded / finished tasks |
| Retry Recovery Rate | Tasks with a failed first attempt and at least 1 retry that eventually succeeded / all such tasks |
| Cost per Successful Task | All spend, including failed attempts and retries / successful tasks |

DDL: [`schema/schema.sql`](../schema/schema.sql) · Queries: [`schema/metrics.sql`](../schema/metrics.sql)
