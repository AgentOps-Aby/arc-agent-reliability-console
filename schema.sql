-- ARC — Agent Reliability Console
-- Data model v1: runs (one row per attempt) and steps (one row per LLM call, tool call or output check)
-- Portable SQL: runs on SQLite, DuckDB and Postgres (swap TEXT timestamps for TIMESTAMPTZ on Postgres).
--
-- Terms (fixed for the whole project):
--   Task = one unit of work an agent was asked to do. Identified by task_id.
--   Run  = one attempt at a task. First attempt has retry_count = 0; each retry is a new run.
--   Step = one LLM call, tool call or output check inside a run.

CREATE TABLE runs (
    run_id              TEXT PRIMARY KEY,                 -- 'run_7f3a91'
    task_id             TEXT NOT NULL,                    -- 'tsk_4812'  shared by every attempt at the same task
    parent_run_id       TEXT REFERENCES runs(run_id),     -- NULL on first attempt; else the run this one retried
    retry_count         INTEGER NOT NULL DEFAULT 0        -- 0 = first attempt, 1 = first retry, ...
                        CHECK (retry_count >= 0),
    retry_trigger       TEXT NOT NULL DEFAULT 'none'
                        CHECK (retry_trigger IN ('none', 'auto_policy', 'manual')),

    agent_name          TEXT NOT NULL,                    -- 'support-triage'
    task_type           TEXT NOT NULL,                    -- 'refund_request'
    deploy_version      TEXT NOT NULL,                    -- 'support-triage@v42' (prompt + tools + config bundle)
    model_name          TEXT NOT NULL,                    -- 'claude-sonnet-4-5'
    model_version       TEXT NOT NULL,                    -- '20250929'

    started_at          TEXT NOT NULL,                    -- ISO-8601 UTC
    ended_at            TEXT,                             -- NULL while running or if the run hung

    status              TEXT NOT NULL                     -- runtime view: did the run finish?
                        CHECK (status IN ('running', 'completed', 'errored', 'timed_out', 'cancelled')),
    outcome_check       TEXT NOT NULL DEFAULT 'skipped'   -- task view: did the output pass verification?
                        CHECK (outcome_check IN ('passed', 'failed', 'skipped')),
    failure_category    TEXT                              -- set by the classifier; NULL on success (see docs/failure-taxonomy.md)
                        CHECK (failure_category IS NULL OR failure_category IN (
                            'tool_error', 'timeout', 'rate_limit', 'invalid_tool_args', 'loop_max_steps',
                            'context_overflow', 'guardrail_block', 'output_parse_error',
                            'silent_wrong_output', 'unknown')),
    taxonomy_version    TEXT,                             -- version of classifier/taxonomy.json that set failure_category, e.g. '1.0.0'.
                                                          -- Tells you WHICH rule set labeled the run: if a category's share jumps,
                                                          -- check whether failures changed or the rules did. NULL until classified.
    final_error_message TEXT,                             -- last error surfaced by the run, if any

    step_count          INTEGER NOT NULL DEFAULT 0,       -- rollup from steps
    input_tokens        INTEGER NOT NULL DEFAULT 0,       -- rollup from steps
    output_tokens       INTEGER NOT NULL DEFAULT 0,       -- rollup from steps
    cost_usd            NUMERIC(10, 4) NOT NULL DEFAULT 0, -- rollup from steps (LLM + paid tool calls)

    CHECK ((retry_count = 0 AND parent_run_id IS NULL) OR (retry_count > 0 AND parent_run_id IS NOT NULL))
);

CREATE TABLE steps (
    step_id        TEXT PRIMARY KEY,                      -- 'stp_7f3a91_03'
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    step_index     INTEGER NOT NULL,                      -- 1, 2, 3 ... order within the run
    step_type      TEXT NOT NULL
                   CHECK (step_type IN ('llm_call', 'tool_call', 'output_check')),
    tool_name      TEXT,                                  -- 'stripe.create_refund'; NULL for llm_call
    started_at     TEXT NOT NULL,
    latency_ms     INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout')),
    error_type     TEXT,                                  -- normalized code: 'rate_limit', 'http_5xx', 'schema_invalid' ...
    error_message  TEXT,                                  -- raw message, for humans
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(10, 4) NOT NULL DEFAULT 0,
    UNIQUE (run_id, step_index)
);

-- Audit trail for the failure classifier (Step 6): one row per classified failed run.
-- runs.failure_category holds the label; this table records how it was decided.
CREATE TABLE classifications (
    run_id            TEXT PRIMARY KEY REFERENCES runs(run_id),
    failure_category  TEXT,          -- same value written to runs.failure_category
    layer             TEXT,          -- 'rules' | 'llm'
    confidence        TEXT,          -- rules: 'high' | 'medium'; llm: probability 0..1
    rule_id           TEXT,          -- e.g. 'struct:timeout', 'regex:rate_limit', 'llm:mock'
    taxonomy_version  TEXT,          -- '1.0.0'
    classified_at     TEXT
);

CREATE INDEX idx_runs_task    ON runs (task_id);
CREATE INDEX idx_runs_agent   ON runs (agent_name, started_at);
CREATE INDEX idx_runs_deploy  ON runs (deploy_version);
CREATE INDEX idx_steps_run    ON steps (run_id);
CREATE INDEX idx_steps_tool   ON steps (tool_name, status);

-- Per-agent reliability targets, like an uptime SLO per service in telecom.
-- Error budget = (100 - target_task_success_pct)% of tasks over error_budget_window_days.
CREATE TABLE agent_targets (
    agent_name                        TEXT PRIMARY KEY,            -- 'support-triage'
    target_task_success_pct           NUMERIC(5, 2) NOT NULL,      -- 95.00
    max_cost_per_successful_task_usd  NUMERIC(10, 4) NOT NULL,     -- 0.2500
    error_budget_window_days          INTEGER NOT NULL DEFAULT 30,
    effective_from                    TEXT NOT NULL                -- ISO date; a new row when a target changes
);

-- Success rule (updated in Step 3):
--   completed + passed  -> success (verified)
--   completed + skipped -> success (unverified)  -- counted, but shown separately with check coverage
--   completed + failed  -> silent failure
--   anything else       -> failure
-- "The run finished" is still not the same as "the task succeeded": a failed check always overrides a clean finish.
CREATE VIEW run_results AS
SELECT
    r.*,
    CASE WHEN r.status = 'completed' AND r.outcome_check IN ('passed', 'skipped') THEN 1 ELSE 0 END AS is_success,
    CASE WHEN r.status = 'completed' AND r.outcome_check = 'passed'  THEN 1 ELSE 0 END AS is_verified_success,
    CASE WHEN r.status = 'completed' AND r.outcome_check = 'failed'  THEN 1 ELSE 0 END AS is_silent_failure,
    CASE WHEN r.outcome_check <> 'skipped' THEN 1 ELSE 0 END                            AS is_checked,
    CASE WHEN r.status = 'cancelled' THEN 1 ELSE 0 END                                  AS is_cancelled
FROM runs r;
-- Cancelled runs (stopped on purpose by a person or upstream system) are neither successes nor failures.
-- A task whose last attempt was cancelled, with no success, is excluded from success-rate metrics.

-- One row per task: how its first attempt went and how it ended.
CREATE VIEW task_results AS
SELECT
    task_id,
    MIN(agent_name)                                        AS agent_name,
    MIN(task_type)                                         AS task_type,
    MAX(CASE WHEN retry_count = 0 THEN is_success END)     AS first_attempt_success,
    MAX(is_success)                                        AS eventual_success,
    MAX(is_verified_success)                               AS eventual_verified_success,
    MAX(retry_count)                                       AS retries,
    COUNT(*)                                               AS attempts,
    SUM(cost_usd)                                          AS total_cost_usd,
    MIN(started_at)                                        AS first_started_at,
    MAX(CASE WHEN status = 'running' THEN 1 ELSE 0 END)    AS still_running,
    CASE WHEN MAX(is_success) = 0
          AND MAX(CASE WHEN is_cancelled = 1 THEN retry_count END) = MAX(retry_count)
         THEN 1 ELSE 0 END                                 AS ended_cancelled
FROM run_results
GROUP BY task_id;
