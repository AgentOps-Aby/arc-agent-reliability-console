# ARC failure taxonomy

Version 1.0.0 · generated from [`classifier/taxonomy.json`](../classifier/taxonomy.json). Rules run in order; first match wins; one primary category per failed run.

| # | Code | Definition | Severity | Retry helps? | Example | Remediation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `guardrail_block` | A safety filter, policy check or the model's own refusal stopped the run. | medium | no | `Blocked by output policy: PII detected (credit card number)` | Review a sample for false positives; tune the policy; route to a human. |
| 2 | `context_overflow` | The prompt plus history exceeded the model's context window. | high | no | `400 prompt is too long: 212,480 tokens > 200,000 maximum` | Summarize or trim history; retrieve less; split the task; larger-context model. |
| 3 | `loop_max_steps` | The agent repeated actions without progress until it hit the step limit. | high | no | `AgentError: max_steps (25) reached without final answer` | Detect repeated calls early and stop; lower the step limit; improve tool descriptions and stop conditions. |
| 4 | `rate_limit` | A model or tool provider refused the call for too many requests or exhausted quota. | high | yes | `429 Too Many Requests: rate limit exceeded, retry after 20s` | Exponential backoff with jitter; honor Retry-After; cap concurrency; raise limits. |
| 5 | `invalid_tool_args` | The model called a tool with malformed, wrong-type or made-up arguments, or an unknown tool. | medium | sometimes | `422 Unprocessable: field 'order_id' must match ^ORD-[0-9]{8}$, got 'ORD-4812'` | Return clear validation errors to the model; tighten tool schemas; look up IDs instead of letting the model guess. |
| 6 | `tool_error` | A tool or external API failed for a reason other than rate limits, timeouts or bad arguments. | medium | sometimes | `502 Bad Gateway from billing-api /v2/refunds` | Retry 5xx only, with a cap; circuit breaker per tool; alert the tool owner. |
| 7 | `timeout` | A step or the whole run exceeded its time limit. | medium | sometimes | `Tool call web.fetch exceeded 30000 ms` | Per-tool timeouts; retry once; break long work into steps; investigate p95 latency. |
| 8 | `output_parse_error` | The final output could not be parsed or did not match the required schema. | medium | yes | `JSONDecodeError: Expecting ',' delimiter: line 14 column 3` | Structured output or JSON mode; repair prompt with the parse error; more lenient parser. |
| 9 | `silent_wrong_output` | The run finished cleanly but its output failed verification. | critical | sometimes | `(no error) output_check: refund_amount 412.00 != order_total 41.20` | Add or tighten output checks; feed check result into retry; human review sample; track by deploy_version. |
| 10 | `unknown` | A failed run that no rule above matched. | varies | unknown | `Unhandled exception: KeyError 'line_items'` | Review weekly; add a rule or category when a pattern repeats in >= 3 runs. Keep share < 10% of failures. |

Keep `unknown` under 10% of failed runs. Review unknowns weekly; a pattern seen in 3+ runs gets a rule; split a category when the fix differs; version the taxonomy and re-label 100 runs a quarter (target >= 85% agreement).
