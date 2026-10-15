-- ARC core metrics, computed from the task_results view in schema.sql.
-- Tasks with a run still in progress, or that ended cancelled, are excluded: they are neither successes nor failures.

-- 1. First-Attempt Success Rate: tasks whose first run succeeded / all finished tasks
-- 2. Task Success Rate (TSR):    tasks that eventually succeeded / all finished tasks
-- 3. Retry Recovery Rate:        retried tasks (first attempt failed) that eventually succeeded / all such tasks
-- 4. Cost per Successful Task:   all spend, including failed attempts and retries / successful tasks
SELECT
    agent_name,
    COUNT(*)                                                         AS tasks,
    ROUND(AVG(first_attempt_success) * 100, 1)                       AS first_attempt_success_pct,
    ROUND(AVG(eventual_success) * 100, 1)                            AS task_success_rate_pct,
    ROUND(100.0 * SUM(CASE WHEN first_attempt_success = 0 AND retries > 0 THEN eventual_success ELSE 0 END)
          / NULLIF(SUM(CASE WHEN first_attempt_success = 0 AND retries > 0 THEN 1 ELSE 0 END), 0), 1)
                                                                     AS retry_recovery_rate_pct,
    ROUND(SUM(total_cost_usd) / NULLIF(SUM(eventual_success), 0), 4) AS cost_per_successful_task_usd,
    ROUND(100.0 * SUM(eventual_success - eventual_verified_success)
          / NULLIF(SUM(eventual_success), 0), 1)                     AS unverified_success_share_pct
FROM task_results
WHERE still_running = 0 AND ended_cancelled = 0
GROUP BY agent_name
ORDER BY agent_name;

-- Check coverage: share of finished runs whose output was verified. Shown next to TSR.
SELECT agent_name, ROUND(100.0 * AVG(is_checked), 1) AS check_coverage_pct
FROM run_results WHERE status = 'completed' GROUP BY agent_name;

-- Failure mix: share of failed runs per category (sums to 100% per agent).
SELECT agent_name, failure_category,
       COUNT(*) AS failed_runs,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY agent_name), 1) AS share_pct
FROM run_results WHERE is_success = 0 AND status NOT IN ('running', 'cancelled')
GROUP BY agent_name, failure_category
ORDER BY agent_name, failed_runs DESC;

-- Agent vs target (SLO view): TSR and cost per success against agent_targets.
SELECT t.agent_name,
       ROUND(AVG(t.eventual_success) * 100, 1)                         AS task_success_rate_pct,
       g.target_task_success_pct,
       ROUND(SUM(t.total_cost_usd) / NULLIF(SUM(t.eventual_success), 0), 4) AS cost_per_successful_task_usd,
       g.max_cost_per_successful_task_usd,
       -- error budget used: actual failure share / allowed failure share
       ROUND(100.0 * (1 - AVG(t.eventual_success)) / NULLIF((100 - g.target_task_success_pct) / 100.0, 0), 1)
                                                                       AS error_budget_used_pct
FROM task_results t JOIN agent_targets g USING (agent_name)
WHERE t.still_running = 0 AND t.ended_cancelled = 0
GROUP BY t.agent_name, g.target_task_success_pct, g.max_cost_per_successful_task_usd;

-- Retry chain for one task, walked through parent_run_id (recursive CTE).
WITH RECURSIVE chain AS (
    SELECT run_id, parent_run_id, retry_count, status, outcome_check, cost_usd
    FROM runs WHERE task_id = :task_id AND parent_run_id IS NULL
    UNION ALL
    SELECT r.run_id, r.parent_run_id, r.retry_count, r.status, r.outcome_check, r.cost_usd
    FROM runs r JOIN chain c ON r.parent_run_id = c.run_id
)
SELECT * FROM chain ORDER BY retry_count;
