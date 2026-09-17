#!/usr/bin/env python3
"""No API calls. Checks transcript extraction, request building, thresholds, and the block path with a fake jev."""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jev_stop  # noqa: E402


def line(role, text, **kw):
    if role == "user":
        return json.dumps({"type": "user", "message": {"content": text}, **kw})
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, **kw})


def write_transcript():
    rows = [
        line("user", "old request"),
        line("assistant", "old reply"),
        line("user", "<system-reminder>injected</system-reminder>"),
        line("user", "run the tests"),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}]}}),
        line("assistant", "Done (I did not run the tests)"),
        "broken line{{{",
    ]
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("truncated first line\n" + "\n".join(rows) + "\n")
    f.close()
    return f.name


def test():
    path = write_transcript()
    ctx, last, tools = jev_stop.read_tail(path)
    assert tools == ["Bash: pytest -q → error"], tools
    assert last == "Done (I did not run the tests)", last
    assert ctx == ["run the tests", "old reply", "old request"], ctx  # newest first; injected line and tool_result dropped

    rules = jev_stop.load_rules()
    assert len(rules) >= 3
    body = jev_stop.build_request(rules, ctx, last, tools)
    assert body["state"]["tool calls this turn (oldest first)"] == tools
    assert set(body["questions"]) == {f"r{i}" for i in range(len(rules))} | {"scold"}
    assert rules[0] in body["questions"]["r0"]["instructions"]

    os.environ["LIMPET_BLOCK"] = "hand work=0.5,shall I start=0.6"
    assert jev_stop.threshold("Don't hand work to the human") == 0.5
    assert jev_stop.threshold("Back up claims") == float("inf")
    os.environ["LIMPET_BLOCK"] = "0.8"
    assert jev_stop.threshold("anything") == 0.8
    os.environ["LIMPET_BLOCK"] = ""
    assert jev_stop.threshold("anything") == float("inf")

    # block path with a fake jev: rule 0 hot, others cold
    os.environ["LIMPET_BLOCK"] = "0.8"
    os.environ["AI_GATEWAY_API_KEY"] = "test"
    jev_stop.LOG = os.path.join(tempfile.mkdtemp(), "log.jsonl")
    jev_stop.call = lambda body, key: {"answers": {**{q: {"probability": 0.9 if q == "r0" else 0.1} for q in body["questions"]}},
                                       "usage": {"inputTokens": 1}}
    err = io.StringIO()
    sys.stdin, sys.stderr = io.StringIO(json.dumps({"transcript_path": path})), err
    assert jev_stop.main() == 2
    assert rules[0] in err.getvalue() and "90%" in err.getvalue(), err.getvalue()
    rec = json.loads(open(jev_stop.LOG, encoding="utf-8").read())
    assert rec["blocked"] == [rules[0]] and rec["n_tools"] == 1, rec

    sys.stdin = io.StringIO(json.dumps({"transcript_path": path, "stop_hook_active": True}))
    assert jev_stop.main() == 0  # never push back twice on the same stop

    jev_stop.call = lambda body, key: (_ for _ in ()).throw(TimeoutError("slow"))
    sys.stdin = io.StringIO(json.dumps({"transcript_path": path}))
    assert jev_stop.main() == 0  # API trouble never blocks
    sys.stderr = sys.__stderr__
    print("ok")


if __name__ == "__main__":
    test()
