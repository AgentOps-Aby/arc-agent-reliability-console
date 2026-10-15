#!/usr/bin/env python3
"""
ARC failure classifier (taxonomy v1.0.0).

Layer 1  rules: structured error codes, step status/patterns, regex on messages. Evaluated in taxonomy order,
         first match wins. Each match carries a confidence: high (structured evidence) or medium (regex only).
Layer 2  for runs the rules can't classify (and, with --gate medium, for medium-confidence matches too):
           --llm anthropic : real LLM call with LLM_PROMPT below (needs ANTHROPIC_API_KEY + `pip install anthropic`)
           --llm mock      : (default) stand-in when no API key: a naive Bayes text classifier trained ONLY on the
                             dev split's labeled failures. It plays the LLM's role; its numbers are real for the
                             stand-in, not a claim about what an LLM would score.

Writes runs.failure_category + runs.taxonomy_version and an audit row per run in `classifications`,
then prints an evaluation against ground truth (dev / test / held-out wording).

Usage: python classify.py [--db ../data/arc.db] [--llm mock|anthropic] [--gate none|medium]
"""
import argparse
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict

TAXONOMY_VERSION = "1.0.0"
MAX_STEPS = 25
CONTEXT_LIMIT = 200_000
ORDER = ["guardrail_block", "context_overflow", "loop_max_steps", "rate_limit", "invalid_tool_args",
         "tool_error", "timeout", "output_parse_error", "silent_wrong_output", "unknown"]

# ---------------------------------------------------------------------------------------------
# Layer 1: rules
# ---------------------------------------------------------------------------------------------
CODE_MAP = {  # normalized steps.error_type -> category (structured = high confidence)
    "policy_block": "guardrail_block", "refusal": "guardrail_block",
    "context_length": "context_overflow",
    "max_steps": "loop_max_steps",
    "rate_limit": "rate_limit",
    "unknown_tool": "invalid_tool_args", "not_found": "invalid_tool_args",
    "http_5xx": "tool_error", "http_4xx": "tool_error", "connection_error": "tool_error",
    "timeout": "timeout",
    "parse_error": "output_parse_error",
}
RX = {
    "guardrail_block": r"blocked by .*policy|content policy|policy filter|model declined|can.?t help with|response is a refusal",
    "context_overflow": r"prompt is too long|maximum context length|exceed context limit|context limit|tokens? > [\d,]+ maximum",
    "loop_max_steps": r"max_steps|iteration limit|max(imum)? (number of )?(iterations|steps)",
    "rate_limit": r"\b429\b|rate.?limit|too many requests|quota|retry-after|overloaded",
    "invalid_tool_args": r"\b422\b|unprocessable|must match|validation error for \w+args|input should be a valid|"
                         r"missing required parameter|unknown tool|does not exist|not found in|\b404\b",
    "tool_error": r"\b5\d\d\b|bad gateway|service unavailable|internal server error|connection (reset|refused)|"
                  r"econnrefused|econnreset|\b401\b|\b403\b|unauthorized|forbidden",
    "timeout": r"timed? ?out|deadline ?exceeded|exceeded \d+ ?ms|timeouterror",
    "output_parse_error": r"jsondecodeerror|outputparserexception|could not parse|validation errors? for finaloutput",
}
RXC = {k: re.compile(v, re.I) for k, v in RX.items()}


def features(run, steps):
    failing = [s for s in steps if s["status"] != "ok"]
    fs = failing[-1] if failing else None
    check = next((s for s in steps if s["step_type"] == "output_check"), None)
    text = " | ".join(t for t in [fs["error_message"] if fs else None, run["final_error_message"],
                                  check["error_message"] if check else None] if t)
    tools = [s["tool_name"] for s in steps if s["step_type"] == "tool_call"]
    longest = cur = 0
    for i, t in enumerate(tools):
        cur = cur + 1 if i and t == tools[i - 1] else 1
        longest = max(longest, cur)
    return dict(fs=fs, check=check, text=text, repeat=longest)


SPECIFIC_CODES = {"policy_block", "refusal", "context_length", "max_steps", "rate_limit", "unknown_tool",
                  "not_found", "timeout", "parse_error", "schema_invalid", "connection_error"}


def rules(run, f):
    """Return (category, confidence, rule_id) or (None, None, None).

    Two tiers of evidence, each resolved in taxonomy order (first match wins):
      high   = specific error code, step status, or run structure (step limit + repeated tool, completed + failed check)
      medium = regex on the error text, generic codes (http_5xx/http_4xx/internal_error), or token counts alone
    Specific evidence beats medium evidence; within a tier, taxonomy order decides.
    """
    fs = f["fs"]
    code = fs["error_type"] if fs else None
    st = fs["step_type"] if fs else None
    err_text = " | ".join(t for t in [fs["error_message"] if fs else None, run["final_error_message"]] if t)
    check_text = f["check"]["error_message"] if f["check"] and f["check"]["error_message"] else ""
    hits = {c for c, rx in RXC.items() if rx.search(err_text)}
    struct = set()
    if code in SPECIFIC_CODES:
        if code == "schema_invalid":
            struct.add("invalid_tool_args" if st == "tool_call" else "output_parse_error")
        elif code == "not_found":
            if st == "tool_call":
                struct.add("invalid_tool_args")
        else:
            struct.add(CODE_MAP[code])
    elif code in ("http_5xx", "http_4xx"):
        hits.add("tool_error")
    if fs and st == "llm_call" and (fs["input_tokens"] or 0) > CONTEXT_LIMIT:
        hits.add("context_overflow")
    if run["step_count"] >= MAX_STEPS and f["repeat"] >= 3:
        struct.add("loop_max_steps")
    if (fs and fs["status"] == "timeout") or run["status"] == "timed_out":
        struct.add("timeout")
    if fs and st == "output_check":
        struct.add("output_parse_error")
    if run["status"] == "completed" and run["outcome_check"] == "failed" and not fs:
        struct.add("guardrail_block" if RXC["guardrail_block"].search(check_text) else "silent_wrong_output")
    if st != "tool_call":
        hits.discard("invalid_tool_args")
    if st != "output_check":
        hits.discard("output_parse_error")
    for tier, conf, ev in ((struct, "high", "struct"), (hits, "medium", "regex")):
        for cat in ORDER:
            if cat in tier:
                return cat, conf, f"{ev}:{cat}"
    return None, None, None


# ---------------------------------------------------------------------------------------------
# Layer 2: LLM prompt (real) and naive Bayes stand-in (mock)
# ---------------------------------------------------------------------------------------------
LLM_PROMPT = """You classify failed runs of production AI agents into exactly one failure category.

Categories (pick the ROOT CAUSE; if several apply, prefer the earlier one in this list):
1. guardrail_block      - a safety filter, policy check or the model's own refusal stopped the run
2. context_overflow     - prompt + history exceeded the model's context window
3. loop_max_steps       - the agent repeated actions without progress until a step/turn limit
4. rate_limit           - a provider refused the call for too many requests or exhausted quota (even if wrapped in a 5xx)
5. invalid_tool_args    - the model called a tool with malformed, wrong-type or made-up arguments, or a non-existent tool
6. tool_error           - a tool/API failed for another reason (5xx, network, auth, outage)
7. timeout              - a step or run exceeded its time limit ("context deadline exceeded" is a TIMEOUT, not context)
8. output_parse_error   - the final output could not be parsed or did not match the required schema
9. silent_wrong_output  - the run completed with no errors but its output failed verification
10. unknown             - none of the above fits with reasonable confidence

Run:
{run_json}

Reply with JSON only: {{"category": "<one code>", "confidence": <0.0-1.0>, "evidence": "<the words or fields that decided it>"}}"""


def run_context(run, f):
    fs = f["fs"]
    return json.dumps({
        "agent": run["agent_name"], "status": run["status"], "outcome_check": run["outcome_check"],
        "step_count": run["step_count"], "max_steps": MAX_STEPS, "longest_repeated_tool_streak": f["repeat"],
        "failing_step": None if not fs else {k: fs[k] for k in ("step_type", "tool_name", "status", "error_type",
                                                                 "error_message", "input_tokens", "latency_ms")},
        "final_error_message": run["final_error_message"],
        "output_check_detail": f["check"]["error_message"] if f["check"] else None,
    }, indent=1)


def tokens(run, f):
    fs = f["fs"]
    t = (f["text"] or "").lower()
    words = re.findall(r"[a-z_]+|\d{3}", t)
    grams = [t[i:i + 4] for i in range(max(0, len(t) - 3))]
    meta = [f"status={run['status']}", f"check={run['outcome_check']}",
            f"step={fs['step_type'] if fs else 'none'}", f"sstatus={fs['status'] if fs else 'none'}",
            f"code={fs['error_type'] if fs else 'none'}", f"atmax={run['step_count'] >= MAX_STEPS}"]
    return words + grams + meta * 3


class NaiveBayes:
    def fit(self, docs, labels):
        self.cls = Counter(labels)
        self.tok = defaultdict(Counter)
        for d, y in zip(docs, labels):
            self.tok[y].update(d)
        self.vocab = set(t for c in self.tok.values() for t in c)
        self.tot = {c: sum(v.values()) for c, v in self.tok.items()}
        return self

    def predict(self, d):
        n, V = sum(self.cls.values()), len(self.vocab)
        scores = {}
        for c in self.cls:
            s = math.log(self.cls[c] / n)
            for t in d:
                if t in self.vocab:
                    s += math.log((self.tok[c][t] + 1) / (self.tot[c] + V))
            scores[c] = s
        m = max(scores.values())
        z = sum(math.exp(v - m) for v in scores.values())
        best = max(scores, key=scores.get)
        return best, 1 / z


def llm_anthropic(prompt):   # pragma: no cover  (needs a key)
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(model="claude-haiku-4-5", max_tokens=150,
                                 messages=[{"role": "user", "content": prompt}])
    out = json.loads(msg.content[0].text)
    return out["category"], float(out.get("confidence", 0.5))


# ---------------------------------------------------------------------------------------------
def load(db):
    db.row_factory = sqlite3.Row
    runs = [dict(r) for r in db.execute("""
        SELECT r.*, g.true_failure_category truth, g.split, g.holdout_message
        FROM run_results r JOIN ground_truth g USING(run_id)
        WHERE r.is_success = 0 AND r.status NOT IN ('running','cancelled')""")]
    steps = defaultdict(list)
    for s in db.execute("SELECT * FROM steps ORDER BY run_id, step_index"):
        steps[s["run_id"]].append(dict(s))
    return runs, steps


def evaluate(rows, label):
    n = len(rows)
    acc = sum(r["pred"] == r["truth"] for r in rows) / n if n else 0
    print(f"  {label:<34} n={n:>4}  accuracy {100 * acc:5.1f}%")
    return acc


def per_category(rows):
    out = []
    for c in ORDER:
        tp = sum(r["pred"] == c and r["truth"] == c for r in rows)
        fp = sum(r["pred"] == c and r["truth"] != c for r in rows)
        fn = sum(r["pred"] != c and r["truth"] == c for r in rows)
        p = tp / (tp + fp) if tp + fp else None
        rc = tp / (tp + fn) if tp + fn else None
        out.append(dict(category=c, support=tp + fn, predicted=tp + fp,
                        precision=None if p is None else round(100 * p, 1),
                        recall=None if rc is None else round(100 * rc, 1)))
    return out


def main(db_path, llm, gate):
    db = sqlite3.connect(db_path)
    runs, steps = load(db)
    feats = {r["run_id"]: features(r, steps[r["run_id"]]) for r in runs}

    # Layer 1
    t0 = time.perf_counter()
    for r in runs:
        r["pred"], r["conf"], r["rule"] = rules(r, feats[r["run_id"]])
        r["rule_pred"] = r["pred"] or "unknown"     # rules-only baseline: unmatched -> unknown
    rule_ms = (time.perf_counter() - t0) * 1000 / len(runs)

    # Layer 2 setup: train the stand-in on DEV only
    dev = [r for r in runs if r["split"] == "dev"]
    nb = NaiveBayes().fit([tokens(r, feats[r["run_id"]]) for r in dev], [r["truth"] for r in dev])

    for r in runs:
        r["layer"] = "rules"
        if r["pred"] is None or (gate == "medium" and r["conf"] == "medium"):
            f = feats[r["run_id"]]
            if llm == "anthropic":
                cat, p = llm_anthropic(LLM_PROMPT.format(run_json=run_context(r, f)))
            else:
                cat, p = nb.predict(tokens(r, f))
            if p < 0.5:
                cat = "unknown"
            r["pred"], r["conf"], r["rule"], r["layer"] = cat, round(p, 3), f"llm:{llm}", "llm"

    # ---- write back -----------------------------------------------------------------------------
    db.execute("""CREATE TABLE IF NOT EXISTS classifications (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), failure_category TEXT, layer TEXT,
                    confidence TEXT, rule_id TEXT, taxonomy_version TEXT, classified_at TEXT)""")
    db.execute("DELETE FROM classifications")
    db.execute("UPDATE runs SET failure_category = NULL, taxonomy_version = NULL")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for r in runs:
        db.execute("UPDATE runs SET failure_category = ?, taxonomy_version = ? WHERE run_id = ?",
                   (r["pred"], TAXONOMY_VERSION, r["run_id"]))
        db.execute("INSERT INTO classifications VALUES (?,?,?,?,?,?,?)",
                   (r["run_id"], r["pred"], r["layer"], str(r["conf"]), r["rule"], TAXONOMY_VERSION, now))
    db.commit()

    # ---- evaluation -------------------------------------------------------------------------------
    test = [r for r in runs if r["split"] == "test"]
    print(f"\n=== ARC classifier v{TAXONOMY_VERSION}  (layer 2 = {llm}, gate = {gate}) ===")
    print(f"failed runs classified: {len(runs)}  (dev {len(dev)}, test {len(test)})")
    res = dict(llm=llm, gate=gate, n=len(runs))
    res["accuracy"] = {
        "dev": evaluate(dev, "dev (rules written against it)"),
        "test": evaluate(test, "test (held out)"),
        "test_holdout_wording": evaluate([r for r in test if r["holdout_message"]], "test, held-out wording only"),
        "test_seen_wording": evaluate([r for r in test if not r["holdout_message"]], "test, familiar wording only"),
    }
    res["accuracy"]["test_rules_only"] = evaluate([dict(r, pred=r["rule_pred"]) for r in test], "test, rules only (no layer 2)")
    res["accuracy"]["test_holdout_rules_only"] = evaluate([dict(r, pred=r["rule_pred"]) for r in test if r["holdout_message"]],
                                                          "test held-out wording, rules only")
    for layer in ("rules", "llm"):
        rows = [r for r in test if r["layer"] == layer]
        res[f"test_{layer}_share"] = round(100 * len(rows) / len(test), 1)
        res[f"test_{layer}_accuracy"] = round(100 * sum(r["pred"] == r["truth"] for r in rows) / len(rows), 1) if rows else None
        print(f"  test handled by {layer:<6} {res[f'test_{layer}_share']:5.1f}%  accuracy {res[f'test_{layer}_accuracy']}%")
    for conf in ("high", "medium"):
        rows = [r for r in test if r["conf"] == conf]
        if rows:
            print(f"  rules confidence {conf:<7} n={len(rows):>3}  accuracy {100 * sum(r['pred'] == r['truth'] for r in rows) / len(rows):5.1f}%")
    res["per_category_test"] = per_category(test)
    print("\n  per category (test)        support  predicted  precision  recall")
    for c in res["per_category_test"]:
        print(f"  {c['category']:<26}{c['support']:>8}{c['predicted']:>11}{str(c['precision']):>11}{str(c['recall']):>8}")
    cm = {t: Counter(r["pred"] for r in test if r["truth"] == t) for t in ORDER}
    res["confusion_test"] = [dict(truth=t, pred=p, n=cm[t][p]) for t in ORDER for p in ORDER if cm[t][p]]
    print("\n  confusion matrix (test): rows = truth, cols = predicted (abbrev.)")
    ab = {c: c[:5] for c in ORDER}
    print("  " + " " * 21 + "".join(f"{ab[c]:>7}" for c in ORDER))
    for t in ORDER:
        print(f"  {t:<21}" + "".join(f"{(cm[t][p] or '.'):>7}" for p in ORDER))
    errs = Counter((r["truth"], r["pred"]) for r in test if r["truth"] != r["pred"])
    res["top_errors_test"] = [dict(truth=a, pred=b, n=n) for (a, b), n in errs.most_common(8)]
    print("\n  top test errors:", res["top_errors_test"])
    res["rule_latency_ms_per_run"] = round(rule_ms, 4)
    prompt_tokens = sum(len(LLM_PROMPT.format(run_json=run_context(r, feats[r["run_id"]]))) / 4 for r in runs) / len(runs)
    res["llm_prompt_tokens_est"] = round(prompt_tokens)
    print(f"\n  rules latency {rule_ms:.3f} ms/run (measured) | LLM prompt ~{prompt_tokens:.0f} tokens/run (est.)")

    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), f"classifier_eval_{llm}_{gate}.json")
    for r in runs:
        r.pop("rule_pred", None)
    with open(path, "w") as fh:
        json.dump(res, fh, indent=2)
    print(f"  wrote {path}")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "arc.db"))
    ap.add_argument("--llm", choices=["mock", "anthropic"], default="mock")
    ap.add_argument("--gate", choices=["none", "medium"], default="none",
                    help="none: Layer 2 only for runs no rule matched; medium: also for regex-only (medium) matches")
    a = ap.parse_args()
    main(a.db, a.llm, a.gate)
