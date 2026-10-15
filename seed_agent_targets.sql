-- The four v1 agents and their targets (set in Step 4 so each agent tells a different story; see docs/answer-key.md).
-- Must match TARGETS in generator/generate_data.py and data/agent_targets.csv.
INSERT INTO agent_targets (agent_name, target_task_success_pct, max_cost_per_successful_task_usd, error_budget_window_days, effective_from) VALUES
  ('support-triage', 92.00, 0.0450, 30, '2026-09-01'),  -- customer support: healthy
  ('code-fix',       83.00, 1.0000, 30, '2026-09-01'),  -- coding: meets TSR, over cost cap
  ('web-research',   93.00, 0.4500, 30, '2026-09-01'),  -- research/browsing: budget blown by bad deploy
  ('invoice-entry',  97.00, 0.0400, 30, '2026-09-01');  -- data entry/workflow: budget blown by one task type
