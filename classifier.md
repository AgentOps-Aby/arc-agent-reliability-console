# ARC failure classifier

`classifier/classify.py` labels every failed run with one of the 10 taxonomy codes, writes `runs.failure_category` +
`runs.taxonomy_version = '1.0.0'`, and records how each label was decided in the `classifications` table.

```
python generator/generate_data.py      # data (seed 42)
python classifier/classify.py          # Layer 1 rules + Layer 2 (default: mock stand-in, gate = none)
python classifier/classify.py --llm anthropic   # real LLM for Layer 2 (needs ANTHROPIC_API_KEY, pip install anthropic)
python analysis/compute_metrics.py     # metrics now use classifier labels
```

## How it works

1. **Layer 1, rules.** Reads the failing step (last step with status ≠ ok), run fields and error text.
   *High* evidence = specific `error_type`, step status, run structure (step limit + repeated tool; completed + failed check).
   *Medium* evidence = regex on text, generic codes (`http_5xx`, `http_4xx`), token counts alone.
   High beats medium; within a tier the taxonomy order decides (first match wins).
2. **Layer 2, LLM.** Only for runs no rule matched (`--gate none`), or also regex-only matches (`--gate medium`).
   Prompt = taxonomy with precedence order and known traps + compact JSON of the run → `{category, confidence, evidence}`.
   Confidence < 0.5 → `unknown` (review queue). Without an API key, the stand-in is a naive Bayes text model trained only
   on the dev split.

## Results (test split = 20% of tasks, scored once; 102 of 225 failures use held-out wording)

| Setting | Rules share | Test accuracy | Held-out wording | Layer 2 cost / month* |
| --- | --- | --- | --- | --- |
| Rules only | 100% | 93.8% | 86.3% | $0 |
| Gate none (default) | 88.0% | **97.8%** | **95.1%** | ~$0.08 |
| Gate medium | 71.1% | 98.2% | 96.1% | ~$0.20 |

\*~1,035 failed runs/month, claude-haiku-4-5 at $1 / $5 per M input / output tokens, ~460 + 40 tokens per call ≈ $0.00066.
Rules: 0.06 ms per run (measured). Coverage (named category) 92.0% vs 90% target; accuracy 97.8% vs 85% target.

Misses on test: 3 invalid_tool_args with new date-format wording → unknown; 1 rate limit ("SlowDown") with generic
`http_5xx` code → tool_error; 1 rate limit → unknown.

**Caveats.** The same person wrote the messages, rules and held-out set, so these are optimistic upper bounds.
The classifier can't see the 26 wrong outputs that skipped their output check (they look like successes).
Next: provider error adapters so `error_type` is reliable, real LLM evaluated on held-out wording, weekly review queue
for low-confidence labels, hand-labeled real failures as the true test set.
