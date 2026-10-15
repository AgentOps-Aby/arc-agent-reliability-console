#!/usr/bin/env python3
"""
ARC synthetic data generator.

Generates ~5,000 agent runs over 30 days (2026-09-01 .. 2026-09-30) for ARC's four agents,
matching schema/schema.sql (Step 2) and classifier/taxonomy.json (Step 3).

Outputs (in --out, default ../data):
  runs.csv           one row per attempt (failure_category / taxonomy_version left NULL: the classifier fills them)
  steps.csv          one row per LLM call, tool call or output check
  agent_targets.csv  per-agent SLO targets
  ground_truth.csv   the TRUE failure category, planted-pattern tags and dev/test split per run (answer key; not used by ARC)
  arc.db             SQLite database built from schema.sql with all of the above loaded

Planted patterns (see docs/answer-key.md):
  P1  Bad deploy web-research@v18 on day 12 spikes timeouts; fixed by v19 on day 15
  P2  Retries fix most rate-limit and timeout failures, almost never invalid (hallucinated) tool arguments
  P3  invoice-entry / vendor_onboarding succeeds far less often than every other task type
  P4  code-fix context_overflow creeps up over the month as repository context grows
  B1  Month-end ERP rate-limit burst for invoice-entry (days 29-30, 14:00-16:00 UTC): a retry storm
  B2  Wrong outputs on runs with no output check look like successes (only ground truth knows)

Usage:  python generate_data.py [--seed 42] [--out ../data]
"""
import argparse
import csv
import math
import os
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

START = datetime(2026, 9, 1, tzinfo=timezone.utc)   # day 1
DAYS = 30
END = START + timedelta(days=DAYS)
MAX_STEPS = 25
CONTEXT_LIMIT = 200_000

CATEGORIES = ["tool_error", "timeout", "rate_limit", "invalid_tool_args", "loop_max_steps",
              "context_overflow", "guardrail_block", "output_parse_error", "silent_wrong_output", "unknown"]

# ---------------------------------------------------------------------------------------------
# Agent configuration. fail = first-attempt probability of each TRUE failure category.
# ---------------------------------------------------------------------------------------------
AGENTS = {
    "support-triage": dict(
        tasks_per_day=56, model=("claude-haiku-4-5", "20251001"), price=(1.0, 5.0),
        task_types={"refund_request": 0.35, "order_status": 0.30, "account_update": 0.20, "escalation_summary": 0.15},
        tools=["orders-api.get_order", "billing-api.create_refund", "crm.update_contact", "kb.search"],
        steps=(3, 9), base_in=3200, check_rate=0.72,
        deploys=[(1, 0, "support-triage@v41"), (9, 15, "support-triage@v42")],
        fail=dict(tool_error=.012, timeout=.008, rate_limit=.014, invalid_tool_args=.018, loop_max_steps=.004,
                  context_overflow=.001, guardrail_block=.007, output_parse_error=.006, silent_wrong_output=.014,
                  unknown=.004)),
    "code-fix": dict(
        tasks_per_day=32, model=("claude-sonnet-4-5", "20250929"), price=(3.0, 15.0),
        task_types={"bug_fix": 0.40, "test_generation": 0.25, "dependency_upgrade": 0.20, "lint_cleanup": 0.15},
        tools=["repo.read_file", "repo.search", "sandbox.run_tests", "git.create_pr"],
        steps=(6, 16), base_in=9000, check_rate=0.95,
        deploys=[(1, 0, "code-fix@v7"), (20, 18, "code-fix@v8")],
        fail=dict(tool_error=.018, timeout=.022, rate_limit=.010, invalid_tool_args=.025, loop_max_steps=.030,
                  context_overflow=.004, guardrail_block=.002, output_parse_error=.012, silent_wrong_output=.045,
                  unknown=.008)),
    "web-research": dict(
        tasks_per_day=36, model=("claude-sonnet-4-5", "20250929"), price=(3.0, 15.0),
        task_types={"competitor_brief": 0.35, "fact_check": 0.40, "market_scan": 0.25},
        tools=["web.search", "web.fetch", "browser.extract"],
        steps=(5, 13), base_in=6000, check_rate=0.55,
        deploys=[(1, 0, "web-research@v17"), (12, 10, "web-research@v18"), (15, 16, "web-research@v19")],
        fail=dict(tool_error=.025, timeout=.022, rate_limit=.016, invalid_tool_args=.008, loop_max_steps=.016,
                  context_overflow=.008, guardrail_block=.008, output_parse_error=.008, silent_wrong_output=.028,
                  unknown=.005)),
    "invoice-entry": dict(
        tasks_per_day=43, model=("claude-haiku-4-5", "20251001"), price=(1.0, 5.0),
        task_types={"invoice_capture": 0.50, "po_match": 0.35, "vendor_onboarding": 0.15},
        tools=["ocr.extract", "erp.lookup_vendor", "erp.create_invoice"],
        steps=(3, 7), base_in=2500, check_rate=0.92,
        deploys=[(1, 0, "invoice-entry@v3"), (18, 13, "invoice-entry@v4")],
        fail=dict(tool_error=.006, timeout=.004, rate_limit=.006, invalid_tool_args=.006, loop_max_steps=.002,
                  context_overflow=.001, guardrail_block=.002, output_parse_error=.006, silent_wrong_output=.005,
                  unknown=.002)),
}

# P3: vendor_onboarding adds these on top of the invoice-entry baseline
VENDOR_ONBOARDING_EXTRA = dict(invalid_tool_args=.15, silent_wrong_output=.08, output_parse_error=.05, tool_error=.02)

TARGETS = [  # agent, target TSR %, max cost per successful task, window, effective_from
    # Set so each agent tells a different story on the dashboard (see docs/answer-key.md):
    ("support-triage", 92.0, 0.0450, 30, "2026-09-01"),   # healthy: inside both targets
    ("code-fix",       83.0, 1.0000, 30, "2026-09-01"),   # meets TSR, but cost per success breaks the cap as context grows (P4)
    ("web-research",   93.0, 0.4500, 30, "2026-09-01"),   # misses TSR, almost entirely from the 3-day bad deploy (P1)
    ("invoice-entry",  97.0, 0.0400, 30, "2026-09-01"),   # misses TSR badly, driven by one task type (P3)
]

TOOL_COST = {"web.search": 0.005, "ocr.extract": 0.010, "sandbox.run_tests": 0.002}
TOOL_LATENCY = {"web.fetch": 1800, "web.search": 700, "browser.extract": 2500, "sandbox.run_tests": 14000,
                "repo.read_file": 120, "repo.search": 400, "git.create_pr": 900, "ocr.extract": 2200,
                "erp.lookup_vendor": 350, "erp.create_invoice": 600, "orders-api.get_order": 180,
                "billing-api.create_refund": 450, "crm.update_contact": 300, "kb.search": 250}
TOOL_TIMEOUT_MS = {"sandbox.run_tests": 120000}

# Retry success probability given the TRUE category of the failed attempt (P2 lives here)
RETRY_SUCCESS = dict(rate_limit=.86, timeout=.78, tool_error=.55, output_parse_error=.72, invalid_tool_args=.06,
                     loop_max_steps=.10, context_overflow=.03, unknown=.35, silent_wrong_output=.30)
INCIDENT_TIMEOUT_RETRY_SUCCESS = .18    # retrying into a bad deploy rarely helps
BURST_RATE_LIMIT_RETRY_SUCCESS = .35    # retries during a burst hit the same wall (retry storm)


# ---------------------------------------------------------------------------------------------
# Raw error messages. Several variants per category, some deliberately misleading for Step 6.
# ---------------------------------------------------------------------------------------------
def order_id(rng):
    return f"ORD-{rng.randint(10**7, 10**8 - 1)}"


def msg(cat, rng, tool, step_type, tokens=None):
    """Return (error_type_code, raw_message, http_status_or_None) for a failing step."""
    t = tool or "llm"
    if cat == "tool_error":
        v = rng.choice([
            ("http_5xx", f"502 Bad Gateway from {t}"),
            ("http_5xx", f"{t} returned 503 Service Unavailable"),
            ("http_5xx", f"HTTPError: 500 Internal Server Error for url: https://internal/{t.replace('.', '/')}"),
            ("connection_error", f"ConnectionResetError: [Errno 104] Connection reset by peer ({t})"),
            ("connection_error", f"ECONNREFUSED 10.0.4.17:8443 while calling {t}"),
            ("http_4xx", f"401 Unauthorized: service token for {t} expired"),
            ("http_4xx", f"403 Forbidden: {t} denied access to resource"),
        ])
        return v
    if cat == "timeout":
        limit = TOOL_TIMEOUT_MS.get(tool, 30000)
        return rng.choice([
            ("timeout", f"Tool call {t} exceeded {limit} ms"),
            ("timeout", f"ReadTimeout: HTTPSConnectionPool(host='{t.split('.')[0]}.svc'): Read timed out. (read timeout={limit // 1000})"),
            ("timeout", f"rpc error: code = DeadlineExceeded desc = context deadline exceeded ({t})"),   # trap: 'context'
            ("timeout", f"asyncio.TimeoutError after {limit // 1000}s waiting for {t}"),
        ])
    if cat == "rate_limit":
        if step_type == "llm_call":
            return rng.choice([
                ("rate_limit", "429 Too Many Requests: rate_limit_error: Number of request tokens has exceeded your per-minute rate limit"),
                ("rate_limit", "overloaded_error: 529 the model is temporarily overloaded, please retry"),
            ])
        return rng.choice([
            ("rate_limit", f"429 Too Many Requests from {t}: rate limit exceeded, retry after {rng.choice([5, 10, 20, 30])}s"),
            ("rate_limit", f"{t}: quota exceeded for quota metric 'requests per minute'"),
            ("http_5xx", f"{t} returned 503: upstream quota exhausted, Retry-After: 60"),   # trap: looks like tool_error
        ])
    if cat == "invalid_tool_args":
        oid = order_id(rng)
        return rng.choice([
            ("schema_invalid", f"422 Unprocessable Entity: field 'order_id' must match ^ORD-[0-9]{{8}}$, got '{oid[:8]}'"),
            ("schema_invalid", f"ValidationError: 1 validation error for {t.split('.')[-1]}Args\namount\n  Input should be a valid number [type=float_parsing, input_value='forty-one dollars']"),
            ("not_found", f"404 Not Found: record {oid} does not exist"),                         # trap: looks like tool_error
            ("not_found", f"vendor_id 'VND-{rng.randint(1000, 9999)}' not found in ERP"),
            ("unknown_tool", f"Unknown tool '{t.split('.')[0]}.lookup_customer_by_email'; available tools: {t}"),
            ("schema_invalid", f"400 Bad Request: missing required parameter 'currency' in call to {t}"),
        ])
    if cat == "loop_max_steps":
        return rng.choice([
            ("max_steps", f"AgentError: max_steps ({MAX_STEPS}) reached without final answer"),
            ("max_steps", f"Agent stopped: iteration limit of {MAX_STEPS} exceeded"),
        ])
    if cat == "context_overflow":
        n = tokens or rng.randint(201_000, 236_000)
        return rng.choice([
            ("context_length", f"400 invalid_request_error: prompt is too long: {n:,} tokens > {CONTEXT_LIMIT:,} maximum"),
            ("context_length", f"input length and max_tokens exceed context limit: {n} + 4096 > {CONTEXT_LIMIT}"),
            ("internal_error", f"BadRequestError: This model's maximum context length is {CONTEXT_LIMIT} tokens. However, your messages resulted in {n} tokens"),
        ])
    if cat == "guardrail_block":
        return rng.choice([
            ("policy_block", "Blocked by output policy: PII detected (credit card number)"),
            ("policy_block", "Request rejected by content policy filter (category: self_harm=low, pii=high)"),
            ("refusal", "Model declined: I'm sorry, but I can't help with sharing another customer's account details."),
        ])
    if cat == "output_parse_error":
        return rng.choice([
            ("parse_error", f"JSONDecodeError: Expecting ',' delimiter: line {rng.randint(3, 40)} column {rng.randint(1, 30)}"),
            ("schema_invalid", "ValidationError: 2 validation errors for FinalOutput\nline_items\n  Field required [type=missing]"),  # trap: same word as invalid args
            ("parse_error", "OutputParserException: Could not parse LLM output: `Here is the summary you asked for:`"),
        ])
    if cat == "unknown":
        return rng.choice([
            ("internal_error", "KeyError: 'line_items'"),
            ("internal_error", "RuntimeError: Event loop is closed"),
            (None, "TypeError: 'NoneType' object is not subscriptable"),
            ("internal_error", "worker exited with code 137"),
            (None, "Unhandled exception in agent runtime (see logs)"),
        ])
    raise ValueError(cat)


def check_detail(agent, rng):
    """Detail text an output check writes when the output is wrong (silent_wrong_output)."""
    return rng.choice({
        "support-triage": ["output_check: refund_amount 412.00 != order_total 41.20",
                           "output_check: reply cites policy KB-114 which does not exist",
                           "output_check: ticket closed but customer question unanswered"],
        "code-fix": ["output_check: 3 of 41 tests failing after patch",
                     "output_check: PR modifies files outside task scope (12 files changed)",
                     "output_check: tests pass but fix does not address reported stack trace"],
        "web-research": ["output_check: 2 of 6 cited URLs return 404",
                         "output_check: claimed figure '38%' not found in any cited source",
                         "output_check: brief covers 2 of 4 requested competitors"],
        "invoice-entry": ["output_check: invoice total 1,250.00 != sum(line_items) 1,520.00",
                          "output_check: tax_id format invalid for country DE",
                          "output_check: duplicate invoice INV-20931 already posted"],
    }[agent])


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(dt):
    return (dt - START).days + 1


def deploy_at(cfg, dt):
    version = cfg["deploys"][0][2]
    for d, hour, v in cfg["deploys"]:
        if START + timedelta(days=d - 1, hours=hour) <= dt:
            version = v
    return version


def in_incident(agent, dt):   # P1 window: v18 live
    return agent == "web-research" and deploy_at(AGENTS[agent], dt) == "web-research@v18"


def in_burst(agent, dt):      # B1 window
    return agent == "invoice-entry" and day_of(dt) in (29, 30) and 14 <= dt.hour < 16


def context_growth(agent, day):   # P4: repository context grows over the month
    return 1.0 + 0.65 * (day - 1) / (DAYS - 1) if agent == "code-fix" else 1.0


def pick(rng, weights):
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def first_attempt_probs(agent, task_type, dt):
    cfg = AGENTS[agent]
    p = dict(cfg["fail"])
    day = day_of(dt)
    if agent == "invoice-entry" and task_type == "vendor_onboarding":
        for k, v in VENDOR_ONBOARDING_EXTRA.items():
            p[k] += v
    if in_incident(agent, dt):
        p["timeout"] += 0.32
    if agent == "code-fix":
        p["context_overflow"] = 0.004 + 0.085 * ((day - 1) / (DAYS - 1)) ** 1.4
    if in_burst(agent, dt):
        p["rate_limit"] += 0.45
    return p


# ---------------------------------------------------------------------------------------------
# One run = a list of steps + a run row
# ---------------------------------------------------------------------------------------------
class Gen:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.runs, self.steps, self.truth = [], [], []
        self.n_run = 0

    def new_run_id(self):
        self.n_run += 1
        return "run_" + format(self.rng.getrandbits(24), "06x") + format(self.n_run, "04d")

    def make_run(self, agent, task_type, task_id, started, retry_count, parent, trigger, true_cat, cancelled=False):
        rng, cfg = self.rng, AGENTS[agent]
        run_id = self.new_run_id()
        day = day_of(started)
        growth = context_growth(agent, day)
        pin, pout = cfg["price"]
        checked = rng.random() < cfg["check_rate"]
        lo, hi = cfg["steps"]
        n = MAX_STEPS if true_cat == "loop_max_steps" else rng.randint(lo, hi)
        if cancelled:
            n = rng.randint(1, max(1, n // 2))

        # which step fails, and what kind of step it is
        fail_idx = None
        if true_cat in ("tool_error", "timeout", "invalid_tool_args", "unknown"):
            fail_idx = rng.randint(2, max(2, n))
        elif true_cat == "rate_limit":
            fail_idx = rng.randint(1, max(1, n))
        elif true_cat in ("context_overflow", "guardrail_block"):
            fail_idx = max(1, n - rng.randint(0, 2))
        elif true_cat == "loop_max_steps":
            fail_idx = n

        t = started
        steps, in_tot, out_tot, cost_tot = [], 0, 0, 0.0
        loop_tool = rng.choice(cfg["tools"])
        refusal_completed = true_cat == "guardrail_block" and rng.random() < 0.4   # refusal that "completes"
        if refusal_completed:
            fail_idx = None
        status, final_err, outcome = "completed", None, "skipped"

        for i in range(1, n + 1):
            if true_cat == "loop_max_steps" and i > 6:
                stype = "llm_call" if i % 2 == 1 else "tool_call"
                tool = loop_tool if stype == "tool_call" else None
            else:
                stype = "llm_call" if i % 2 == 1 else "tool_call"
                tool = rng.choice(cfg["tools"]) if stype == "tool_call" else None
            if fail_idx == i:
                if true_cat in ("context_overflow", "guardrail_block"):
                    stype, tool = "llm_call", None
                elif true_cat in ("tool_error", "timeout", "invalid_tool_args") and stype == "llm_call":
                    stype, tool = "tool_call", rng.choice(cfg["tools"])
                elif true_cat == "rate_limit" and stype == "tool_call" and rng.random() < 0.3:
                    stype, tool = "llm_call", None
                if true_cat == "timeout" and in_incident(agent, t):
                    stype, tool = "tool_call", "web.fetch"

            # tokens, latency, cost
            itok = otok = 0
            if stype == "llm_call":
                itok = int(cfg["base_in"] * growth * (1 + 0.45 * (i - 1)) * rng.lognormvariate(0, 0.25))
                otok = int(rng.uniform(120, 900))
                lat = int(rng.lognormvariate(math.log(1600 + itok / 25), 0.35))
                if true_cat == "context_overflow" and fail_idx == i:
                    itok = rng.randint(201_000, 236_000)
                    otok, lat = 0, int(rng.uniform(300, 900))
            else:
                base = TOOL_LATENCY.get(tool, 500)
                if tool == "web.fetch" and in_incident(agent, t):
                    base *= 6          # P1: v18 removed the fetch cache, every fetch is slow
                lat = int(rng.lognormvariate(math.log(base), 0.5))
            st_status, etype, emsg = "ok", None, None

            if fail_idx == i:
                etype, emsg = msg(true_cat, rng, tool, stype, tokens=itok if true_cat == "context_overflow" else None)
                st_status = "timeout" if true_cat == "timeout" else "error"
                if true_cat == "timeout":
                    lat = TOOL_TIMEOUT_MS.get(tool, 30000) + rng.randint(0, 400)
                if true_cat == "rate_limit":
                    lat, otok = int(rng.uniform(40, 300)), 0
                    itok = 0
                # make classification non-trivial: normalized code not always present
                r = rng.random()
                if r < 0.18:
                    etype = None
                elif r < 0.33:
                    etype = "internal_error" if stype == "llm_call" else rng.choice(["http_error", "internal_error"])

            step_cost = itok * pin / 1e6 + otok * pout / 1e6 + (TOOL_COST.get(tool, 0) if stype == "tool_call" else 0)
            steps.append(dict(step_id=f"stp_{run_id[4:]}_{i:02d}", run_id=run_id, step_index=i, step_type=stype,
                              tool_name=tool, started_at=iso(t), latency_ms=lat, status=st_status, error_type=etype,
                              error_message=emsg, input_tokens=itok, output_tokens=otok, cost_usd=round(step_cost, 6)))
            in_tot, out_tot, cost_tot = in_tot + itok, out_tot + otok, cost_tot + step_cost
            t += timedelta(milliseconds=lat + rng.randint(20, 400))
            if fail_idx == i:
                status = "timed_out" if true_cat == "timeout" else "errored"
                final_err = emsg if rng.random() < 0.5 else f"AgentRunError: step {i} ({tool or 'llm_call'}) failed: {emsg}"
                break

        # output check / parser step at the end of runs that reached a final answer
        if status == "completed" and not cancelled:
            needs_parse_step = true_cat == "output_parse_error"
            if checked or needs_parse_step or refusal_completed:
                i = len(steps) + 1
                lat = int(rng.uniform(80, 1500))
                st_status, etype, emsg = "ok", None, None
                if true_cat == "output_parse_error":
                    etype, emsg = msg("output_parse_error", rng, None, "output_check")
                    st_status, status, outcome = "error", "errored", "failed"
                    final_err = emsg
                elif refusal_completed:
                    emsg = "output_check: response is a refusal: \"I'm sorry, but I can't help with that request.\""
                    outcome = "failed"
                elif true_cat == "silent_wrong_output":
                    emsg, outcome = check_detail(agent, rng), "failed"
                else:
                    outcome = "passed"
                steps.append(dict(step_id=f"stp_{run_id[4:]}_{i:02d}", run_id=run_id, step_index=i,
                                  step_type="output_check", tool_name=None, started_at=iso(t), latency_ms=lat,
                                  status=st_status, error_type=etype, error_message=emsg, input_tokens=0,
                                  output_tokens=0, cost_usd=0.0))
                t += timedelta(milliseconds=lat)
        if cancelled:
            status, outcome, final_err = "cancelled", "skipped", "Run cancelled by user"

        ended = t
        if ended > END:   # still going when the data window closes
            status, ended, outcome, final_err = "running", None, "skipped", None

        if status == "running" or cancelled:
            truth_outcome = status
        elif true_cat is None:
            truth_outcome = "success"
        elif true_cat == "silent_wrong_output" and outcome == "skipped":
            truth_outcome = "wrong_unchecked"   # B2: looks like a success, isn't
        else:
            truth_outcome = "failure"

        tags = []
        if in_incident(agent, started):
            tags.append("P1_bad_deploy")
        if true_cat == "invalid_tool_args" and retry_count > 0:
            tags.append("P2_wasted_retry")
        if agent == "invoice-entry" and task_type == "vendor_onboarding":
            tags.append("P3_low_task_type")
        if agent == "code-fix" and true_cat == "context_overflow":
            tags.append("P4_context_creep")
        if in_burst(agent, started):
            tags.append("B1_month_end_burst")
        if truth_outcome == "wrong_unchecked":
            tags.append("B2_unchecked_wrong")

        self.runs.append(dict(
            run_id=run_id, task_id=task_id, parent_run_id=parent, retry_count=retry_count, retry_trigger=trigger,
            agent_name=agent, task_type=task_type, deploy_version=deploy_at(cfg, started),
            model_name=cfg["model"][0], model_version=cfg["model"][1], started_at=iso(started),
            ended_at=iso(ended) if ended else None, status=status, outcome_check=outcome,
            failure_category=None, taxonomy_version=None, final_error_message=final_err,
            step_count=len(steps), input_tokens=in_tot, output_tokens=out_tot, cost_usd=round(cost_tot, 4)))
        self.steps.extend(steps)
        self.truth.append(dict(run_id=run_id,
                               true_failure_category=true_cat if truth_outcome in ("failure", "wrong_unchecked") else None,
                               true_outcome=truth_outcome, planted_patterns=";".join(tags) or None))
        return run_id, status, outcome, ended

    # -----------------------------------------------------------------------------------------
    def task(self, agent, task_id, started):
        rng, cfg = self.rng, AGENTS[agent]
        task_type = pick(rng, cfg["task_types"])
        cancelled = rng.random() < 0.012
        probs = first_attempt_probs(agent, task_type, started)
        r, acc, cat = rng.random(), 0.0, None
        for k in CATEGORIES:
            acc += probs[k]
            if r < acc:
                cat = k
                break
        if cancelled:
            cat = None

        parent, retry_count, t = None, 0, started
        while True:
            run_id, status, outcome, ended = self.make_run(agent, task_type, task_id, t, retry_count, parent,
                                                           "none" if retry_count == 0 else trigger, cat, cancelled)
            if status in ("completed", "running", "cancelled") and not (status == "completed" and outcome == "failed"):
                return
            # --- retry policy -------------------------------------------------------------
            if retry_count >= 2:
                return
            if status in ("errored", "timed_out"):
                if cat == "guardrail_block" or rng.random() > 0.92:   # naive auto policy retries almost everything
                    return
                trigger, delay = "auto_policy", timedelta(seconds=rng.uniform(2, 90) * (retry_count + 1))
            else:   # completed but check failed: sometimes a human retries it
                if rng.random() > 0.15:
                    return
                trigger, delay = "manual", timedelta(minutes=rng.uniform(30, 300))
            t = ended + delay
            if t >= END:
                return
            # does the retry work?
            p_ok = RETRY_SUCCESS.get(cat, 0.3)
            if cat == "timeout" and in_incident(agent, t):
                p_ok = INCIDENT_TIMEOUT_RETRY_SUCCESS
            if cat == "rate_limit" and in_burst(agent, t):
                p_ok = BURST_RATE_LIMIT_RETRY_SUCCESS
            if rng.random() < p_ok:
                cat = None
            elif rng.random() > 0.85:   # occasionally fails a different way
                cat = pick(rng, first_attempt_probs(agent, task_type, t))
            parent, retry_count = run_id, retry_count + 1

    def generate(self):
        rng = self.rng
        hour_w = [1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8, 7, 8, 8, 8, 7, 6, 5, 4, 3, 2, 2, 1]
        n_task = 0
        for d in range(DAYS):
            day_start = START + timedelta(days=d)
            weekend = day_start.weekday() >= 5
            for agent, cfg in AGENTS.items():
                mean = cfg["tasks_per_day"] * (0.55 if weekend else 1.0)
                if agent == "invoice-entry" and d + 1 >= 28:
                    mean *= 1.8   # month-end close volume
                count = max(0, int(rng.gauss(mean, math.sqrt(mean))))
                for _ in range(count):
                    hour = rng.choices(range(24), weights=hour_w)[0]
                    if agent == "invoice-entry" and d + 1 in (29, 30) and rng.random() < 0.35:
                        hour = rng.choice([14, 15])   # B1: month-end batch lands in one window
                    started = day_start + timedelta(hours=hour, seconds=rng.uniform(0, 3599))
                    n_task += 1
                    self.task(agent, f"tsk_{n_task:05d}", started)
        self.runs.sort(key=lambda r: r["started_at"])


# ---------------------------------------------------------------------------------------------
# Output + summary
# ---------------------------------------------------------------------------------------------
RUN_COLS = ["run_id", "task_id", "parent_run_id", "retry_count", "retry_trigger", "agent_name", "task_type",
            "deploy_version", "model_name", "model_version", "started_at", "ended_at", "status", "outcome_check",
            "failure_category", "taxonomy_version", "final_error_message", "step_count", "input_tokens",
            "output_tokens", "cost_usd"]
STEP_COLS = ["step_id", "run_id", "step_index", "step_type", "tool_name", "started_at", "latency_ms", "status",
             "error_type", "error_message", "input_tokens", "output_tokens", "cost_usd"]


def apply_holdout(g, seed):
    """Put 20% of tasks in a 'test' split and rewrite ~50% of their failing-step messages with held-out phrasings
    (generator/holdout_messages.py). Uses its own RNG and runs after generation, so the main data stream, and every
    number in the answer key, is unchanged."""
    from holdout_messages import holdout_msg
    hr = random.Random(seed + 1000)
    test_tasks = {t for t in sorted({r["task_id"] for r in g.runs}) if hr.random() < 0.20}
    truth = {t["run_id"]: t for t in g.truth}
    steps_by_run = defaultdict(list)
    for st in g.steps:
        steps_by_run[st["run_id"]].append(st)
    for r in sorted(g.runs, key=lambda x: x["run_id"]):
        tr = truth[r["run_id"]]
        tr["split"] = "test" if r["task_id"] in test_tasks else "dev"
        tr["holdout_message"] = 0
        cat = tr["true_failure_category"]
        if tr["split"] != "test" or cat is None:
            continue
        failing = [st for st in steps_by_run[r["run_id"]] if st["status"] != "ok" and st["error_message"]]
        if not failing or hr.random() >= 0.5:
            continue
        st = failing[-1]
        new = holdout_msg(cat, hr, st["tool_name"], st["step_type"], tokens=st["input_tokens"] or None)
        if not new:
            continue
        old = st["error_message"]
        st["error_message"] = new
        fe = r["final_error_message"] or ""
        r["final_error_message"] = fe.replace(old, new) if old in fe else new
        tr["holdout_message"] = 1


def write_csv(path, rows, cols):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r[c]) for c in cols})


def build_db(path, g, root):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(open(os.path.join(root, "schema", "schema.sql")).read())
    db.executemany("INSERT INTO agent_targets VALUES (?,?,?,?,?)", TARGETS)
    # parents before children so the foreign key order is natural
    order = sorted(g.runs, key=lambda r: (r["retry_count"], r["started_at"]))
    db.executemany(f"INSERT INTO runs ({','.join(RUN_COLS)}) VALUES ({','.join('?' * len(RUN_COLS))})",
                   [[r[c] for c in RUN_COLS] for r in order])
    db.executemany(f"INSERT INTO steps ({','.join(STEP_COLS)}) VALUES ({','.join('?' * len(STEP_COLS))})",
                   [[s[c] for c in STEP_COLS] for s in g.steps])
    db.execute("CREATE TABLE ground_truth (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), "
               "true_failure_category TEXT, true_outcome TEXT, planted_patterns TEXT, "
               "split TEXT CHECK (split IN ('dev','test')), holdout_message INTEGER)")
    db.executemany("INSERT INTO ground_truth VALUES (?,?,?,?,?,?)",
                   [[t["run_id"], t["true_failure_category"], t["true_outcome"], t["planted_patterns"],
                     t["split"], t["holdout_message"]] for t in g.truth])
    db.commit()
    return db


def pct(a, b):
    return f"{100.0 * a / b:5.1f}%" if b else "   n/a"


def summary(db, root):
    q = lambda s: db.execute(s).fetchall()
    print("\n=== ARC synthetic dataset ===")
    (runs, tasks, steps) = q("SELECT (SELECT COUNT(*) FROM runs),(SELECT COUNT(DISTINCT task_id) FROM runs),(SELECT COUNT(*) FROM steps)")[0]
    print(f"runs {runs:,} | tasks {tasks:,} | steps {steps:,} | {START.date()} .. {(END - timedelta(days=1)).date()}")
    for row in q("SELECT status, COUNT(*) FROM runs GROUP BY status ORDER BY 2 DESC"):
        print(f"  status {row[0]:<10} {row[1]:>6,}")

    print("\n--- Core metrics by agent (schema/metrics.sql, query 1) vs targets ---")
    metrics = open(os.path.join(root, "schema", "metrics.sql")).read().split(";")
    print(f"{'agent':<15}{'tasks':>6}{'1st-try':>9}{'TSR':>8}{'target':>8}{'retry-rec':>11}{'$/success':>11}{'cap':>8}{'budget used':>13}")
    slo = {r[0]: r for r in db.execute(metrics[3]).fetchall()}
    for r in db.execute(metrics[0]).fetchall():
        s = slo[r[0]]
        print(f"{r[0]:<15}{r[1]:>6}{r[2]:>8.1f}%{r[3]:>7.1f}%{s[2]:>7.0f}%{r[4]:>10.1f}%{r[5]:>11.4f}{s[4]:>8.3f}{s[5]:>12.0f}%")

    print("\n--- True failure mix (ground truth, failed runs) ---")
    total_fail = q("SELECT COUNT(*) FROM ground_truth WHERE true_outcome='failure'")[0][0]
    for cat, n in q("SELECT true_failure_category, COUNT(*) FROM ground_truth WHERE true_outcome='failure' GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {cat:<22}{n:>5}  {pct(n, total_fail)}")

    print("\n--- P1 bad deploy: web-research first-attempt timeout rate by deploy ---")
    for v, n, t in q("""SELECT r.deploy_version, COUNT(*), SUM(g.true_failure_category='timeout')
                        FROM runs r JOIN ground_truth g USING(run_id)
                        WHERE r.agent_name='web-research' AND r.retry_count=0 GROUP BY 1 ORDER BY 1"""):
        print(f"  {v:<20} runs {n:>5}  timeout {pct(t, n)}")

    print("\n--- P2 Retry Recovery Rate by TRUE category of the first attempt (tasks that were retried) ---")
    rows = q("""WITH first AS (
                  SELECT r.task_id, g.true_failure_category AS cat
                  FROM runs r JOIN ground_truth g USING(run_id)
                  WHERE r.retry_count = 0 AND g.true_outcome = 'failure')
                SELECT f.cat, COUNT(*), SUM(t.eventual_success),
                       ROUND(SUM((SELECT SUM(cost_usd) FROM runs x WHERE x.task_id = f.task_id AND x.retry_count > 0)), 2)
                FROM first f JOIN task_results t USING(task_id)
                WHERE t.retries > 0 AND t.still_running = 0 AND t.ended_cancelled = 0
                GROUP BY 1 ORDER BY SUM(t.eventual_success) * 1.0 / COUNT(*) DESC""")
    for cat, n, ok, cost in rows:
        print(f"  {cat:<22} retried tasks {n:>4}  recovered {pct(ok, n)}  retry spend ${cost:>7.2f}")

    split = q("""SELECT COALESCE(g.planted_patterns, '') LIKE '%P1%', COUNT(*), SUM(t.eventual_success)
                 FROM runs r JOIN ground_truth g USING(run_id) JOIN task_results t USING(task_id)
                 WHERE r.retry_count = 0 AND g.true_failure_category = 'timeout' AND g.true_outcome = 'failure'
                   AND t.retries > 0 AND t.still_running = 0 AND t.ended_cancelled = 0 GROUP BY 1 ORDER BY 1""")
    for inside, n, ok in split:
        print(f"    timeout {'during bad deploy (v18)' if inside else 'outside bad deploy   '}  retried tasks {n:>4}  recovered {pct(ok, n)}")

    print("\n--- P3 task success rate by task type (eventual, finished tasks) ---")
    for a, tt, n, s in q("""SELECT agent_name, task_type, COUNT(*), SUM(eventual_success) FROM task_results
                            WHERE still_running=0 AND ended_cancelled=0 GROUP BY 1,2 ORDER BY SUM(eventual_success)*1.0/COUNT(*)"""):
        print(f"  {a:<15}{tt:<20} tasks {n:>4}  TSR {pct(s, n)}")

    print("\n--- P4 code-fix: context_overflow share of first attempts, and avg input tokens per run, by 6-day period ---")
    for b, n, c, tok in q("""SELECT (CAST(julianday(substr(r.started_at,1,10)) - julianday('2026-09-01') AS INT))/6 + 1,
                                    COUNT(*), SUM(g.true_failure_category='context_overflow'), AVG(r.input_tokens)
                             FROM runs r JOIN ground_truth g USING(run_id)
                             WHERE r.agent_name='code-fix' AND r.retry_count=0 GROUP BY 1"""):
        print(f"  days {6 * b - 5:>2}-{6 * b:<2}  runs {n:>4}  overflow {pct(c, n)}  avg input tokens {tok:>9,.0f}")

    print("\n--- Bonus ---")
    b1 = q("""SELECT COUNT(*), SUM(retry_count>0) FROM runs r JOIN ground_truth g USING(run_id)
              WHERE g.planted_patterns LIKE '%B1%'""")[0]
    print(f"  B1 month-end burst window: {b1[0]} runs, {b1[1]} of them retries")
    b2 = q("SELECT COUNT(*) FROM ground_truth WHERE true_outcome='wrong_unchecked'")[0][0]
    print(f"  B2 wrong outputs hidden as unverified successes: {b2}")
    sp = q("""SELECT split, COUNT(*), SUM(true_outcome='failure'), SUM(holdout_message) FROM ground_truth GROUP BY 1""")
    print("\n--- Classifier dev/test split (Step 6) ---")
    for split, n, f, h in sp:
        print(f"  {split:<5} runs {n:>5}  failed {f:>4}  with held-out message {h:>4}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    args = ap.parse_args()
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    g = Gen(args.seed)
    g.generate()
    apply_holdout(g, args.seed)
    write_csv(os.path.join(out, "runs.csv"), g.runs, RUN_COLS)
    write_csv(os.path.join(out, "steps.csv"), g.steps, STEP_COLS)
    write_csv(os.path.join(out, "ground_truth.csv"), g.truth,
              ["run_id", "true_failure_category", "true_outcome", "planted_patterns", "split", "holdout_message"])
    with open(os.path.join(out, "agent_targets.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agent_name", "target_task_success_pct", "max_cost_per_successful_task_usd",
                    "error_budget_window_days", "effective_from"])
        w.writerows(TARGETS)
    db = build_db(os.path.join(out, "arc.db"), g, root)
    summary(db, root)
    print(f"\nWrote {out}/runs.csv, steps.csv, ground_truth.csv, agent_targets.csv, arc.db  (seed {args.seed})")
