#!/usr/bin/env python3
"""No API calls. Transcript extraction for Claude Code and Codex, request building, thresholds, config, and the
block path with a fake jev."""
import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limpet  # noqa: E402

TMP = tempfile.mkdtemp()
limpet.HOME = TMP  # keep the test away from ~/.limpet
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "limpet.py")


def claude_line(role, text, **kw):
    if role == "user":
        return json.dumps({"type": "user", "message": {"content": text}, **kw})
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, **kw})


def codex_line(role, text):
    t = "input_text" if role == "user" else "output_text"
    return json.dumps({"type": "response_item", "payload": {"type": "message", "role": role, "content": [{"type": t, "text": text}]}})


def write(rows, head="truncated first line\n"):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=TMP)
    f.write(head + "\n".join(rows) + "\n")
    f.close()
    return f.name


def test_claude_transcript():
    path = write([
        claude_line("user", "old request"),
        claude_line("assistant", "old reply"),
        claude_line("user", "<system-reminder>injected</system-reminder>"),
        claude_line("user", "run the tests"),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}]}}),
        claude_line("assistant", "Done (I did not run the tests)"),
        "broken line{{{",
    ])
    ctx, last, tools = limpet.read_tail(path)
    assert tools == ["Bash: pytest -q → error"], tools
    assert last == "Done (I did not run the tests)", last
    assert ctx == ["run the tests", "old reply", "old request"], ctx  # newest first; injected line and tool_result dropped
    # a one-line transcript smaller than TAIL_BYTES keeps its first line
    one = write([claude_line("assistant", "only line")], head="")
    assert limpet.read_tail(one)[1] == "only line"
    # a file larger than TAIL_BYTES drops the cut first line
    big = write([claude_line("assistant", "x" * 600_000), claude_line("assistant", "tail")], head="")
    assert limpet.read_tail(big)[1] == "tail"
    return path


def test_codex_transcript():
    path = write([
        codex_line("user", "# AGENTS.md instructions\n\n<INSTRUCTIONS>always answer in English"),
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev prompt"}]}}),
        codex_line("user", "fix the failing test"),
        json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "pytest -q"}}),
        json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "1 failed"}}),
        json.dumps({"type": "response_item", "payload": {"type": "function_call", "call_id": "c2", "name": "apply_patch", "arguments": "{\"path\": \"a.py\"}"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
        codex_line("assistant", "I'll wait for you to confirm before editing."),
    ])
    ctx, last, tools = limpet.read_tail(path)
    assert tools == ["exec: pytest -q → done", "apply_patch: a.py"], tools
    assert last == "I'll wait for you to confirm before editing.", last
    assert ctx == ["fix the failing test"], ctx  # AGENTS.md injection and developer message dropped


def test_config():
    rules = limpet.load_rules()  # first run copies the bundled rules to HOME
    assert os.path.exists(os.path.join(TMP, "rules.md")) and len(rules) >= 3
    qs = limpet.rule_questions(rules, "typesafe")
    assert qs["r0"]["type"] == "noul" and rules[0] in qs["r0"]["instructions"]
    assert limpet.rule_questions(rules, "vercel")["r0"]["type"] == "boolean"
    assert limpet.prob({"type": "noul", "noul": 0.42}, "typesafe") == 0.42
    assert limpet.prob({"type": "boolean", "probability": 0.42}, "vercel") == 0.42
    assert limpet.prob({"probability": "0.9"}, "vercel") is None  # never compare a string with a float

    os.environ["LIMPET_BLOCK"] = "hand work=0.5,shall I start=0.6"
    assert limpet.threshold("Don't hand work to the human") == 0.5
    assert limpet.threshold("Back up claims") == float("inf")
    os.environ["LIMPET_BLOCK"] = "0.8"
    assert limpet.threshold("anything") == 0.8
    os.environ["LIMPET_BLOCK"] = "hand work=high,broken,shall I start=0.6"  # typos are ignored, never fatal
    assert limpet.threshold("Don't hand work") == float("inf") and limpet.threshold("shall I start?") == 0.6
    os.environ["LIMPET_BLOCK"] = ""
    os.environ["CLAUDE_PLUGIN_OPTION_BLOCK"] = "0.7"  # Claude Code plugin user config
    assert limpet.threshold("anything") == 0.7

    with open(os.path.join(TMP, "env"), "w") as f:
        f.write("# comment\nAI_GATEWAY_API_KEY='from-file'\nLIMPET_BLOCK=0.9\n")
    os.environ.pop("AI_GATEWAY_API_KEY", None)
    limpet.load_env(os.path.join(TMP, "env"))
    assert limpet.api_key() == ("vercel", "from-file")
    assert limpet.threshold("x") == 0.7  # plugin config beats the env file
    del os.environ["CLAUDE_PLUGIN_OPTION_BLOCK"]
    os.environ["TYPESAFE_API_KEY"] = "ts"  # a TypeSafe key wins
    assert limpet.api_key() == ("typesafe", "ts")
    del os.environ["TYPESAFE_API_KEY"]
    os.environ.pop("AI_GATEWAY_API_KEY", None)
    os.environ["LIMPET_KEY_CMD"] = "echo from-cmd"
    os.environ["LIMPET_PROVIDER"] = "typesafe"
    assert limpet.api_key() == ("typesafe", "from-cmd")
    del os.environ["LIMPET_KEY_CMD"], os.environ["LIMPET_PROVIDER"]

    rs = ["Run the tests, then, only then, say done", "Run the tests, then, only then, report it", "Answer in Japanese"]
    for r in rs:
        k = limpet.block_key(r, rs)
        assert "," not in k and "=" not in k and k and k in r, (r, k)
    assert limpet.block_key(rs[2], rs) == "Answer"
    assert limpet.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0 and limpet.auroc([0.5], [0.5]) == 0.5
    return rules


def test_block_path(path, rules):
    os.environ["LIMPET_BLOCK"] = "0.8"
    os.environ["AI_GATEWAY_API_KEY"] = "test"
    os.environ["LIMPET_LOG"] = os.path.join(TMP, "log.jsonl")
    limpet.call = lambda state, qs, key, provider: {"answers": {q: {"probability": 0.9 if q == "r0" else 0.1} for q in qs},
                                                    "usage": {"inputTokens": 1}}
    err = io.StringIO()
    sys.stdin, sys.stderr = io.StringIO(json.dumps({"transcript_path": path})), err
    assert limpet.main() == 2
    assert rules[0] in err.getvalue() and "90%" in err.getvalue(), err.getvalue()
    rec = json.loads(open(os.environ["LIMPET_LOG"], encoding="utf-8").read())
    assert rec["blocked"] == [rules[0]] and rec["n_tools"] == 1 and rec["agent"] == "claude" and rec["provider"] == "vercel", rec

    # Codex passes last_assistant_message and turn_id; transcript may be missing
    sys.stdin = io.StringIO(json.dumps({"turn_id": "t", "transcript_path": None, "last_assistant_message": "Shall I proceed?"}))
    assert limpet.main() == 2
    rec = json.loads(open(os.environ["LIMPET_LOG"], encoding="utf-8").read().splitlines()[-1])
    assert rec["agent"] == "codex" and rec["assistant_text"] == "Shall I proceed?", rec

    sys.stdin = io.StringIO(json.dumps({"transcript_path": path, "stop_hook_active": True}))
    assert limpet.main() == 0  # never push back twice on the same stop

    limpet.call = lambda state, qs, key, provider: (_ for _ in ()).throw(TimeoutError("slow"))
    sys.stdin = io.StringIO(json.dumps({"transcript_path": path}))
    assert limpet.main() == 0  # API trouble never blocks
    limpet.call = lambda state, qs, key, provider: ["not", "a", "dict"]
    sys.stdin = io.StringIO(json.dumps({"transcript_path": path}))
    assert limpet.main() == 0  # nor a strange response
    sys.stderr = sys.__stderr__

    # the real entry point: broken stdin, missing log dir, relative log path => exit 0, no traceback
    env = {**os.environ, "LIMPET_LOG": "log.jsonl", "LIMPET_RULES": os.path.join(TMP, "rules.md")}
    for stdin in ("", "not json", json.dumps({"transcript_path": "/nonexistent"})):
        r = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=env, cwd=TMP)
        assert r.returncode == 0 and "Traceback" not in r.stderr, (stdin, r.stderr)


def test_suggest_and_calibrate():
    path = write([
        claude_line("user", "fix the login bug"),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "auth.py"}}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}),
        claude_line("assistant", "Found it. Want me to apply the fix?"),
        claude_line("user", "do it"),
        claude_line("assistant", "Applied. Tests pass."),
        claude_line("user", "thanks"),
        claude_line("assistant", "Now the logout bug. Want me to apply it?"),
        claude_line("user", "do it"),
    ])
    stops = limpet.scan(path)
    assert [s["next"] for s in stops] == ["do it", "thanks", "do it"], stops  # a repeated short reply is not a duplicate
    assert stops[0]["tools"] == ["Edit: auth.py → ok"] and stops[0]["context"] == ["fix the login bug"], stops[0]
    limpet.transcript_files = lambda days: [path]
    os.environ["AI_GATEWAY_API_KEY"] = "test"

    seen_states = []

    def fake(state, qs, key, provider):
        seen_states.append(state)
        nxt = qs["reaction"]["instructions"]
        push = "do it" in nxt
        ans = {q: {"probability": (0.9 if push else 0.1) if q == "r0" else 0.5} for q in qs if q.startswith("r")}
        ans["reaction"] = {"choice": "push" if push else "ack", "confidence": 0.9}
        ans["failure"] = {"choice": "handoff", "confidence": 0.8}
        return {"answers": ans}
    limpet.call = fake

    out = io.StringIO()
    sys.stdout = out
    assert limpet.suggest(days=1, limit=10) == 0
    text = out.getvalue()
    assert "push" in text and "handoff: 2 (100%)" in text and "shall I start" in text, text
    assert os.path.exists(os.path.join(TMP, "suggest.md"))
    out = io.StringIO()
    sys.stdout = out
    assert limpet.calibrate(days=1, limit=10) == 0
    sys.stdout = sys.__stdout__
    text = out.getvalue()
    assert "2 stops the human pushed back on, 1 fine" in text and "LIMPET_BLOCK=" in text and "does not separate" in text, text
    assert all("what the human said next" not in s for s in seen_states)  # rule questions never see the reply


if __name__ == "__main__":
    claude_path = test_claude_transcript()
    test_codex_transcript()
    rules = test_config()
    test_block_path(claude_path, rules)
    test_suggest_and_calibrate()
    print("ok")
