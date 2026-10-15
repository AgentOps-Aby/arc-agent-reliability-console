"""
HELD-OUT error message variants for testing the Step 6 classifier.

These phrasings never appear in the 'dev' split. About half of the failing-step messages in the 'test' split
(20% of tasks) are rewritten with them, so classifier accuracy on 'test' measures how well the rules generalize
to wording they were not written against. Don't read this file while writing or tuning classifier rules.
"""
import random

CONTEXT_LIMIT = 200_000


def holdout_msg(cat, rng: random.Random, tool, step_type, tokens=None):
    t = tool or "llm"
    svc = t.split(".")[0]
    if cat == "tool_error":
        return rng.choice([
            f"upstream connect error or disconnect/reset before headers. reset reason: connection failure ({svc})",
            f"SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed calling {t}",
            f'{t} -> HTTP 500 {{"error":"internal","trace_id":"{rng.getrandbits(32):08x}"}}',
            f"Service {svc} is currently down for maintenance",
        ])
    if cat == "timeout":
        return rng.choice([
            f"Request to {t} did not complete within the allotted time",
            "TimeoutError: The read operation timed out",
            f"{t}: no response after 30.0s, giving up",
        ])
    if cat == "rate_limit":
        return rng.choice([
            f"{t}: Throttled (TooManyRequestsException)",
            "SlowDown: Please reduce your request rate.",
            "RESOURCE_EXHAUSTED: Quota exceeded for requests per minute per project",
        ])
    if cat == "invalid_tool_args":
        return rng.choice([
            f"TypeError: {t.split('.')[-1]}() got an unexpected keyword argument 'refund_reason'",
            "Argument 'due_date' expected format YYYY-MM-DD, received '09/31/2026'",
            f"No such customer: 'cus_{rng.getrandbits(40):010x}' (invalid_request_error)",
        ])
    if cat == "loop_max_steps":
        return rng.choice([
            "Run halted: agent repeated the identical action 8 times",
            "Exceeded maximum number of turns (25)",
        ])
    if cat == "context_overflow":
        n = tokens or rng.randint(201_000, 236_000)
        return rng.choice([
            f"Requested {n:,} tokens, which exceeds the model's 200k window",
            "Error code: 400 - {'error': {'code': 'context_length_exceeded'}}",
        ])
    if cat == "guardrail_block":
        return rng.choice([
            "Response withheld: safety system flagged this content",
            "Tool call denied by policy engine: action 'delete_account' requires human approval",
        ])
    if cat == "output_parse_error":
        return rng.choice([
            "Expected object, received string at path $.result",
            "yaml.scanner.ScannerError: mapping values are not allowed here",
        ])
    if cat == "unknown":
        return rng.choice([
            "AttributeError: 'list' object has no attribute 'get'",
            "IndexError: list index out of range",
            "Killed",
        ])
    return None
