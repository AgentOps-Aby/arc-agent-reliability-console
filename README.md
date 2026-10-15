# ARC — Agent Reliability Console

A reliability dashboard for AI agents running in production: it tracks task success against per-agent targets,
auto-classifies failures, and measures whether retries actually help.

**Live dashboard:** open [`dashboard/index.html`](dashboard/index.html) in a browser, or see the hosted version
(GitHub Pages link below once enabled).

> Portfolio project by Apoorva Patil. All data is **synthetic** (5,122 runs, 4 agents, September 2026) with six
> planted incidents and an answer key, so the dashboard and classifier can be graded against ground truth.

## Why

An AI agent can return HTTP 200, spend $4 and still get the task wrong. Standard monitoring counts requests;
ARC asks whether the *task* succeeded, what kind of failure happened, and what it cost.

## What's here

| Folder | What it is |
| --- | --- |
| `schema/` | SQLite schema (runs, steps, agent targets, classifications), metric queries, seed targets |
| `generator/` | Synthetic data generator with planted incidents; held-out error wording for classifier testing |
| `classifier/` | 10-category failure taxonomy and a two-layer classifier (rules first, LLM fallback) |
| `analysis/` | Computes every metric in the spec, including error-budget burn-rate alerts |
| `dashboard/` | Single-file interactive dashboard (no server) and the scripts that build it |
| `data/` | Generated CSVs, the SQLite database, metric snapshots and classifier evaluation |
| `docs/` | Data model, taxonomy, metrics spec, classifier write-up, answer key (spoilers) |

## Results

- **Classifier:** 97.8% accurate on held-out runs, 95.1% on error wording it never saw; rules handle 88% of failures.
- **Retries:** 70% of retry spend bought nothing. Retries fix rate limits 87% of the time, invented tool arguments about 6%.
- **Bad deploy:** a planted bad release ran 78 hours; a deploy canary on the first ~15 runs would flag it in about 7.
- **Recommendations:** five fixes bring all four agents inside target and cut about 22% of agent spend.

## Run it yourself

Requires Python 3.9+ (standard library only).

```bash
python generator/generate_data.py     # build data/ (seed 42, ~3 s)
python classifier/classify.py         # label failures (add --llm anthropic with ANTHROPIC_API_KEY set)
python analysis/compute_metrics.py    # metrics + burn-rate alerts
python dashboard/export_data.py && python dashboard/build.py   # rebuild dashboard/index.html
```

## Design notes

- "Did the run finish" (`status`) is kept separate from "did the task succeed" (`outcome_check`), so silent failures show up.
- North star is task success vs. each agent's target, guarded by first-attempt success, cost per successful task and check coverage.
- Per-agent error budgets and burn-rate alerts are borrowed from network operations.
- The dashboard never reads the answer key; it has to find the planted incidents from the same signals an on-call engineer would see.
