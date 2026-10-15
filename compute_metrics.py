#!/usr/bin/env python3
"""
Compute every metric in docs/metrics-spec.md on data/arc.db and print a report.
Writes data/metrics_snapshot.json for the dashboard (Step 7+).

Category-based metrics read runs.failure_category; until the Step 6 classifier fills it, they fall back to
ground_truth.true_failure_category (COALESCE), and the report marks them "(ground truth)".

Usage: python compute_metrics.py [--db ../data/arc.db]
"""
import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

MAX_STEPS = 25
FINISHED = "still_running = 0 AND ended_cancelled = 0"
CAT = "COALESCE(r.failure_category, g.true_failure_category)"


def pctl(values, p):
    """Nearest-rank percentile (p in 0..100)."""
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))
    return v[k]


def secs(a, b):
    f = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds()


def main(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    q = lambda s, *a: [dict(r) for r in db.execute(s, a).fetchall()]
    out = {}
    classified = q("SELECT COUNT(*) n FROM runs WHERE failure_category IS NOT NULL")[0]["n"] > 0
    src = "classifier" if classified else "ground truth"

    # ---- North star + M1/M2: success rates -------------------------------------------------------
    base = f"""SELECT {{grp}} AS grp, COUNT(*) tasks,
                 ROUND(100.0*AVG(first_attempt_success),1) fasr,
                 ROUND(100.0*AVG(eventual_success),1) tsr,
                 ROUND(100.0*AVG(eventual_verified_success),1) vtsr
               FROM task_results WHERE {FINISHED} GROUP BY 1 ORDER BY 1"""
    out["success_by_agent"] = q(base.format(grp="agent_name"))
    out["success_fleet"] = q(base.format(grp="'fleet'"))[0]
    for r in out["success_by_agent"] + [out["success_fleet"]]:
        r["retry_lift_pts"] = round(r["tsr"] - r["fasr"], 1)

    # ---- M3: failure mix by category and agent (runs) ---------------------------------------------
    out["failure_mix"] = q(f"""SELECT r.agent_name agent, {CAT} category, COUNT(*) failed_runs,
                                 ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (PARTITION BY r.agent_name),1) share_pct
                               FROM run_results r JOIN ground_truth g USING(run_id)
                               WHERE r.is_success = 0 AND r.status NOT IN ('running','cancelled')
                               GROUP BY 1,2 ORDER BY 1,3 DESC""")
    out["failure_mix_fleet"] = q(f"""SELECT {CAT} category, COUNT(*) failed_runs,
                                       ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (),1) share_pct
                                     FROM run_results r JOIN ground_truth g USING(run_id)
                                     WHERE r.is_success = 0 AND r.status NOT IN ('running','cancelled')
                                     GROUP BY 1 ORDER BY 2 DESC""")

    # ---- M4/M5: retry recovery and wasted retry cost, by category of the first failed attempt ----
    first_fail = f"""WITH ff AS (SELECT r.task_id, {CAT} category FROM run_results r JOIN ground_truth g USING(run_id)
                                 WHERE r.retry_count = 0 AND r.is_success = 0 AND r.status NOT IN ('running','cancelled')),
                     rs AS (SELECT task_id, SUM(cost_usd) retry_cost FROM runs WHERE retry_count > 0 GROUP BY 1)"""
    out["retry_by_category"] = q(first_fail + f"""
        SELECT ff.category, COUNT(*) retried_tasks, SUM(t.eventual_success) recovered,
               ROUND(100.0*AVG(t.eventual_success),1) recovery_pct,
               ROUND(SUM(rs.retry_cost),2) retry_spend,
               ROUND(SUM(CASE WHEN t.eventual_success = 0 THEN rs.retry_cost ELSE 0 END),2) wasted_retry_cost
        FROM ff JOIN task_results t USING(task_id) JOIN rs USING(task_id)
        WHERE t.retries > 0 AND t.{FINISHED.replace(' AND ', ' AND t.')}
        GROUP BY 1 ORDER BY recovery_pct DESC""")
    out["retry_by_agent"] = q(f"""WITH rs AS (SELECT task_id, SUM(cost_usd) retry_cost FROM runs WHERE retry_count > 0 GROUP BY 1)
        SELECT t.agent_name agent, COUNT(*) retried_tasks,
               ROUND(100.0*AVG(t.eventual_success),1) recovery_pct,
               ROUND(SUM(rs.retry_cost),2) retry_spend,
               ROUND(SUM(CASE WHEN t.eventual_success = 0 THEN rs.retry_cost ELSE 0 END),2) wasted_retry_cost
        FROM task_results t JOIN rs USING(task_id)
        WHERE t.first_attempt_success = 0 AND t.retries > 0 AND t.{FINISHED.replace(' AND ', ' AND t.')}
        GROUP BY 1 ORDER BY 1""")
    tot = q(f"SELECT ROUND(SUM(total_cost_usd),2) spend FROM task_results WHERE {FINISHED}")[0]["spend"]
    rspend = sum(r["retry_spend"] for r in out["retry_by_agent"])
    wasted = sum(r["wasted_retry_cost"] for r in out["retry_by_agent"])
    out["retry_totals"] = dict(total_spend=tot, retry_spend=round(rspend, 2), wasted_retry_cost=round(wasted, 2),
                               wasted_share_of_retry_pct=round(100 * wasted / rspend, 1),
                               wasted_share_of_total_pct=round(100 * wasted / tot, 1))

    # ---- M6: cost per successful task ---------------------------------------------------------------
    out["cost_per_success"] = q(f"""SELECT t.agent_name agent, ROUND(SUM(t.total_cost_usd),2) spend, SUM(t.eventual_success) successes,
                                      ROUND(SUM(t.total_cost_usd)/SUM(t.eventual_success),4) cost_per_success,
                                      a.max_cost_per_successful_task_usd cap,
                                      ROUND(SUM(t.total_cost_usd)/COUNT(*),4) cost_per_task
                                    FROM task_results t JOIN agent_targets a USING(agent_name)
                                    WHERE t.{FINISHED.replace(' AND ', ' AND t.')} GROUP BY 1 ORDER BY 1""")
    out["cost_per_success_trend"] = q(f"""SELECT agent_name agent,
                                            (CAST(julianday(substr(first_started_at,1,10)) - julianday('2026-09-01') AS INT))/6 + 1 period,
                                            ROUND(SUM(total_cost_usd)/SUM(eventual_success),4) cost_per_success
                                          FROM task_results WHERE {FINISHED} GROUP BY 1,2 ORDER BY 1,2""")

    # ---- M7: latency ------------------------------------------------------------------------------
    runs = q("SELECT agent_name, deploy_version, started_at, ended_at FROM runs WHERE status = 'completed'")
    by_agent = defaultdict(list)
    for r in runs:
        by_agent[r["agent_name"]].append(secs(r["started_at"], r["ended_at"]))
    tasks = q(f"""SELECT t.agent_name, t.first_started_at, MAX(r.ended_at) last_end
                  FROM task_results t JOIN runs r USING(task_id)
                  WHERE t.eventual_success = 1 AND t.{FINISHED.replace(' AND ', ' AND t.')} GROUP BY t.task_id""")
    task_lat = defaultdict(list)
    for t in tasks:
        task_lat[t["agent_name"]].append(secs(t["first_started_at"], t["last_end"]))
    out["latency"] = [dict(agent=a, run_p50_s=round(pctl(v, 50), 1), run_p95_s=round(pctl(v, 95), 1),
                           task_p50_s=round(pctl(task_lat[a], 50), 1), task_p95_s=round(pctl(task_lat[a], 95), 1))
                      for a, v in sorted(by_agent.items())]
    fetch = q("""SELECT r.deploy_version, s.latency_ms FROM steps s JOIN runs r USING(run_id)
                 WHERE s.tool_name = 'web.fetch' AND s.status = 'ok'""")
    fb = defaultdict(list)
    for f in fetch:
        fb[f["deploy_version"]].append(f["latency_ms"])
    out["web_fetch_latency_by_deploy"] = [dict(deploy=d, calls=len(v), p50_ms=pctl(v, 50), p95_ms=pctl(v, 95))
                                          for d, v in sorted(fb.items())]

    # ---- M8: runs hitting max steps ------------------------------------------------------------------
    out["max_steps"] = q(f"""SELECT agent_name agent, COUNT(*) runs, SUM(step_count >= {MAX_STEPS}) at_max,
                               ROUND(100.0*SUM(step_count >= {MAX_STEPS})/COUNT(*),2) at_max_pct,
                               ROUND(SUM(CASE WHEN step_count >= {MAX_STEPS} THEN cost_usd ELSE 0 END),2) at_max_cost,
                               ROUND(100.0*SUM(CASE WHEN step_count >= {MAX_STEPS} THEN cost_usd ELSE 0 END)/SUM(cost_usd),1) at_max_cost_pct
                             FROM runs WHERE status NOT IN ('running','cancelled') GROUP BY 1 ORDER BY 1""")

    # ---- Supporting: error budget, check coverage, unverified share -------------------------------
    out["slo"] = q(f"""SELECT t.agent_name agent, ROUND(100.0*AVG(t.eventual_success),1) tsr, a.target_task_success_pct target,
                         ROUND(100.0*(1-AVG(t.eventual_success))/((100-a.target_task_success_pct)/100.0),0) budget_used_pct
                       FROM task_results t JOIN agent_targets a USING(agent_name)
                       WHERE t.{FINISHED.replace(' AND ', ' AND t.')} GROUP BY 1 ORDER BY 1""")
    out["verification"] = q(f"""SELECT r.agent_name agent, ROUND(100.0*AVG(r.is_checked),1) check_coverage_pct,
                                  (SELECT ROUND(100.0*SUM(eventual_success-eventual_verified_success)/SUM(eventual_success),1)
                                   FROM task_results t WHERE t.agent_name = r.agent_name AND t.{FINISHED.replace(' AND ', ' AND t.')}) unverified_share_pct
                                FROM run_results r WHERE r.status = 'completed' GROUP BY 1 ORDER BY 1""")
    out["category_source"] = src

    # ---- S3: error-budget burn rate (fast 1-day, slow 7-day) ------------------------------------
    # burn rate = failure rate in the window / allowed failure rate (1 - target). 1.0 = on pace to spend
    # exactly the 30-day budget. Data is daily, so (unlike hourly SRE windows) the alerts are:
    #   fast burn: 1-day burn >= 3 on two consecutive days  (budget would be gone in ~10 days, and it isn't a one-day blip)
    #   slow burn: 7-day burn >= 2 over a full 7-day window  (budget gone in ~15 days)
    daily = q(f"""SELECT t.agent_name agent, substr(t.first_started_at,1,10) day, COUNT(*) tasks,
                    SUM(1 - t.eventual_success) failed, (100 - a.target_task_success_pct) / 100.0 allowed
                  FROM task_results t JOIN agent_targets a USING(agent_name)
                  WHERE t.{FINISHED.replace(' AND ', ' AND t.')} GROUP BY 1,2 ORDER BY 1,2""")
    series, alerts = [], []
    by = defaultdict(list)
    for d in daily:
        by[d["agent"]].append(d)
    for agent, days in by.items():
        for i, d in enumerate(days):
            win = days[max(0, i - 6):i + 1]
            b1 = (d["failed"] / d["tasks"]) / d["allowed"]
            b7 = (sum(x["failed"] for x in win) / sum(x["tasks"] for x in win)) / d["allowed"]
            row = dict(agent=agent, day=d["day"], burn_1d=round(b1, 2), burn_7d=round(b7, 2))
            series.append(row)
            prev_b1 = series[-2]["burn_1d"] if i > 0 else 0
            if b1 >= 3 and prev_b1 >= 3:
                alerts.append(dict(row, alert="fast burn (1d >= 3x, 2 days)"))
            elif len(win) == 7 and b7 >= 2:
                alerts.append(dict(row, alert="slow burn (7d >= 2x)"))
    out["burn_rate_daily"] = series
    out["burn_rate_alerts"] = alerts

    # ---- report ---------------------------------------------------------------------------------------
    def table(title, rows, cols):
        print(f"\n{title}")
        print("  " + "".join(f"{c:>{max(10, len(c) + 2)}}" for c in cols))
        for r in rows:
            print("  " + "".join(f"{str(r[c]):>{max(10, len(c) + 2)}}" for c in cols))

    table("North star (VTSR) + M1/M2 success rates, finished tasks", out["success_by_agent"] + [out["success_fleet"]],
          ["grp", "tasks", "fasr", "tsr", "vtsr", "retry_lift_pts"])
    table(f"M3 failure mix, fleet ({src})", out["failure_mix_fleet"], ["category", "failed_runs", "share_pct"])
    table(f"M4/M5 retry recovery and wasted retry cost by first-failure category ({src})", out["retry_by_category"],
          ["category", "retried_tasks", "recovered", "recovery_pct", "retry_spend", "wasted_retry_cost"])
    table("M4/M5 by agent", out["retry_by_agent"], ["agent", "retried_tasks", "recovery_pct", "retry_spend", "wasted_retry_cost"])
    print("  totals:", out["retry_totals"])
    table("M6 cost per successful task", out["cost_per_success"], ["agent", "spend", "successes", "cost_per_success", "cap", "cost_per_task"])
    table("M7 latency (seconds)", out["latency"], ["agent", "run_p50_s", "run_p95_s", "task_p50_s", "task_p95_s"])
    table("M7 web.fetch latency by deploy (ms)", out["web_fetch_latency_by_deploy"], ["deploy", "calls", "p50_ms", "p95_ms"])
    table("M8 runs hitting max steps", out["max_steps"], ["agent", "runs", "at_max", "at_max_pct", "at_max_cost", "at_max_cost_pct"])
    table("Supporting: error budget", out["slo"], ["agent", "tsr", "target", "budget_used_pct"])
    table("Supporting: verification", out["verification"], ["agent", "check_coverage_pct", "unverified_share_pct"])

    print("\nS3 burn-rate alerts (fast: 1d >= 3x two days running; slow: 7d >= 2x over a full week)")
    for a in alerts:
        print(f"  {a['day']}  {a['agent']:<15} {a['alert']:<30} 1d {a['burn_1d']:>5}  7d {a['burn_7d']:>5}")

    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "metrics_snapshot.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "arc.db"))
    main(ap.parse_args().db)
