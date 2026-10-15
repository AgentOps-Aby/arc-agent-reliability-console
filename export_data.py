#!/usr/bin/env python3
"""
Export a compact JSON snapshot of data/arc.db for the ARC dashboard page.

Only what ARC itself can see goes in: runs, steps of failed runs, tool latencies, targets and the
Step 6 classifier labels. Ground truth stays out (the dashboard must find the planted patterns on its own).

Usage: python export_data.py [--db ../data/arc.db] [--out arc_data.json]
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
STATUS = ["completed", "errored", "timed_out", "cancelled", "running"]
OUTCOME = ["passed", "failed", "skipped"]
CATS = ["tool_error", "timeout", "rate_limit", "invalid_tool_args", "loop_max_steps", "context_overflow",
        "guardrail_block", "output_parse_error", "silent_wrong_output", "unknown"]
STEP_TYPES = ["llm_call", "tool_call", "output_check"]
STEP_STATUS = ["ok", "error", "timeout"]
TRIGGERS = ["none", "auto_policy", "manual"]


def minutes(ts):
    return int((datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - START).total_seconds() // 60)


def secs(a, b):
    f = "%Y-%m-%dT%H:%M:%SZ"
    return int((datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds())


def main(db_path, out):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    runs = [dict(r) for r in db.execute("""
        SELECT r.*, c.layer, c.confidence, c.rule_id FROM runs r LEFT JOIN classifications c USING(run_id)
        ORDER BY r.started_at""")]
    agents = sorted({r["agent_name"] for r in runs})
    task_types = sorted({r["task_type"] for r in runs})
    deploys = sorted({r["deploy_version"] for r in runs})
    tools = sorted({s[0] for s in db.execute("SELECT DISTINCT tool_name FROM steps WHERE tool_name IS NOT NULL")})
    tasks = sorted({r["task_id"] for r in runs})
    t_idx = {t: i for i, t in enumerate(tasks)}
    r_idx = {r["run_id"]: i for i, r in enumerate(runs)}

    # deploy change points (first run seen on each version, per agent)
    changes = []
    for d in deploys:
        first = db.execute("SELECT MIN(started_at), agent_name FROM runs WHERE deploy_version = ?", (d,)).fetchone()
        changes.append([agents.index(first[1]), deploys.index(d), minutes(first[0])])

    rows = []
    for r in runs:
        failed = r["failure_category"] is not None
        rows.append([
            r_idx[r["run_id"]], t_idx[r["task_id"]], r["retry_count"], TRIGGERS.index(r["retry_trigger"]),
            agents.index(r["agent_name"]), task_types.index(r["task_type"]), deploys.index(r["deploy_version"]),
            minutes(r["started_at"]), secs(r["started_at"], r["ended_at"]) if r["ended_at"] else -1,
            STATUS.index(r["status"]), OUTCOME.index(r["outcome_check"]),
            CATS.index(r["failure_category"]) if failed else -1,
            round(r["cost_usd"], 4), r["step_count"], r["input_tokens"],
            r_idx[r["parent_run_id"]] if r["parent_run_id"] else -1,
        ])

    # details for failed runs: steps, messages, classifier audit
    detail = {}
    steps_by_run = {}
    for s in db.execute("SELECT * FROM steps ORDER BY run_id, step_index"):
        steps_by_run.setdefault(s["run_id"], []).append(s)
    tool_lat = []
    for run_id, steps in steps_by_run.items():
        for s in steps:
            if s["step_type"] == "tool_call" and s["status"] == "ok":
                tool_lat.append([r_idx[run_id], tools.index(s["tool_name"]), s["latency_ms"]])
    for r in runs:
        if r["failure_category"] is None:
            continue
        st = steps_by_run.get(r["run_id"], [])
        detail[r_idx[r["run_id"]]] = {
            "id": r["run_id"], "task": r["task_id"], "err": r["final_error_message"],
            "layer": r["layer"], "conf": r["confidence"], "rule": r["rule_id"], "tax": r["taxonomy_version"],
            "steps": [[STEP_TYPES.index(s["step_type"]), tools.index(s["tool_name"]) if s["tool_name"] else -1,
                       s["latency_ms"], STEP_STATUS.index(s["status"]), s["error_message"], s["input_tokens"],
                       round(s["cost_usd"], 5)] for s in st],
        }
    # run ids for every run (for retry-chain display)
    ids = [r["run_id"] for r in runs]
    targets = {r["agent_name"]: [r["target_task_success_pct"], r["max_cost_per_successful_task_usd"]]
               for r in db.execute("SELECT * FROM agent_targets")}
    tool_timeout = {t: (120000 if t == "sandbox.run_tests" else 30000) for t in tools}

    data = dict(
        generated="2026-09-30", start="2026-09-01", days=30, max_steps=25,
        agents=agents, task_types=task_types, deploys=deploys, tools=tools, cats=CATS,
        status=STATUS, outcome=OUTCOME, step_types=STEP_TYPES, step_status=STEP_STATUS, triggers=TRIGGERS,
        targets=[targets[a] for a in agents], tool_timeout=[tool_timeout[t] for t in tools],
        task_ids=tasks, run_ids=ids, deploy_changes=changes,
        runs_cols=["i", "task", "retry", "trigger", "agent", "tt", "deploy", "start_min", "dur_s", "status",
                   "outcome", "cat", "cost", "steps", "in_tok", "parent"],
        runs=rows, detail=detail, tool_lat=tool_lat,
    )
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {out}: {os.path.getsize(out) / 1e6:.2f} MB, {len(rows)} runs, {len(detail)} failed-run details, "
          f"{len(tool_lat)} tool latencies")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(here, "..", "data", "arc.db"))
    ap.add_argument("--out", default=os.path.join(here, "arc_data.json"))
    a = ap.parse_args()
    main(a.db, a.out)
