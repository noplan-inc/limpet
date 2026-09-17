#!/usr/bin/env python3
"""limpet: a Claude Code Stop hook that stops the agent from stopping too early.

When Claude Code is about to stop, this reads the tail of the transcript and sends the last few turns,
the tool calls of the current turn, and the final message to jev (TypeSafe AI's evaluation model, via
Vercel AI Gateway). jev returns, for every rule in rules.md, the probability that the rule was violated.
If any rule is over its threshold, the hook exits 2 with a message telling the agent to keep working.

    echo '{"transcript_path": "...", "stop_hook_active": false}' | python3 jev_stop.py
    python3 jev_stop.py --stats      # per-rule percentiles from the log, to pick thresholds

Environment:
    AI_GATEWAY_API_KEY  Vercel AI Gateway key. Or LIMPET_KEY_CMD: a shell command that prints the key.
    LIMPET_BLOCK        "0.8" (one threshold for all rules) or "run the tests=0.5,hand work=0.6"
                        (a substring of the rule text => its threshold). Unset = shadow mode: log, never block.
    LIMPET_RULES        rules file (default: rules.md next to this script)
    LIMPET_LOG          log file (default: ~/.limpet/log.jsonl)
    LIMPET_JEV_MODEL    default typesafe-ai/jev

No key, API down, or timeout => exit 0 silently. A hook must never stop the work.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
MODEL = os.environ.get("LIMPET_JEV_MODEL", "typesafe-ai/jev")
RULES = os.environ.get("LIMPET_RULES", os.path.join(HERE, "rules.md"))
LOG = os.path.expanduser(os.environ.get("LIMPET_LOG", "~/.limpet/log.jsonl"))
TIMEOUT = 20
TAIL_BYTES = 512 * 1024  # only the tail of the transcript is read
N_CONTEXT = 3  # previous messages sent as context
MAXLEN = 2000  # per message
MAX_TOOLS = 40
MAX_TOOL_STR = 200
SCOLD_Q = "After this message, will the human scold or correct the agent?"

# user lines that look human but are injected by the harness (collected from real transcripts)
NOT_HUMAN_PREFIXES = (
    "<teammate-message", "<system-reminder", "<command-name>", "<command-message", "<local-command-caveat",
    "<local-command-stdout", "<user-prompt-submit-hook", "Another Claude session sent",
    "The coordinator sent a message", "[SYSTEM NOTIFICATION", "[Request interrupted", "[Image:",
    "Stop hook feedback:", "Caveat: The messages below", "API Error",
    "The previous response failed to produce a valid tool call",
)


def load_rules(path=RULES):
    with open(path, encoding="utf-8") as f:
        return [l[2:].strip() for l in f if l.startswith("- ")]


def api_key():
    k = os.environ.get("AI_GATEWAY_API_KEY")
    cmd = os.environ.get("LIMPET_KEY_CMD")
    if not k and cmd:
        try:
            k = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            k = None
    return k or None


# --- transcript ---

def human_text(d):
    """Text the human typed, or None for tool results and harness-injected lines."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        return None
    s = c.strip()
    if not s or s.startswith(NOT_HUMAN_PREFIXES) or re.match(r"<[a-z][a-z-]*[ >]", s):
        return None
    return s


def assistant_text(d):
    if d.get("type") != "assistant":
        return None
    c = (d.get("message") or {}).get("content")
    if not isinstance(c, list):
        return None
    s = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text").strip()
    return s or None


def tool_line(name, inp):
    inp = inp or {}
    v = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("url") \
        or next((x for x in inp.values() if isinstance(x, str)), "")
    s = f"{name}: {v}".strip()
    return s if len(s) <= MAX_TOOL_STR else s[:MAX_TOOL_STR] + "…"


def tools_since_turn(rows):
    """Tool calls since the last human message, oldest first: "Bash: pytest -q → ok" / "→ error" / "→ rejected"."""
    start = 0
    for j in range(len(rows) - 1, -1, -1):
        if human_text(rows[j]):
            start = j
            break
    uses, results = [], {}
    for d in rows[start:]:
        for b in (d.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if d.get("type") == "assistant" and b.get("type") == "tool_use":
                uses.append((b.get("id"), tool_line(b.get("name"), b.get("input"))))
            elif d.get("type") == "user" and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = ("rejected" if d.get("toolDenialKind") == "user-rejected"
                                                 else "denied" if d.get("toolDenialKind")
                                                 else "error" if b.get("is_error") else "ok")
    return [f"{line} → {results[i]}" if i in results else line for i, line in uses][-MAX_TOOLS:]


def read_tail(path):
    """(previous messages newest first, final assistant message, tool calls of this turn)."""
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    rows = []
    for l in lines:
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    texts = [s for d in rows for s in [assistant_text(d) or human_text(d)] if s]
    if not texts:
        return [], None, []
    *ctx, last = texts
    return [c[:MAXLEN] for c in ctx[::-1][:N_CONTEXT]], last[:MAXLEN], tools_since_turn(rows)


# --- jev ---

def build_request(rules, context, last, tools=None):
    state = {"previous messages (newest first)": context, "tool calls this turn (oldest first)": tools or [],
             "agent's final message before stopping": last}
    qs = {f"r{i}": {"type": "boolean", "instructions": f"Is the agent violating this rule? Rule: {r}"}
          for i, r in enumerate(rules)}
    qs["scold"] = {"type": "boolean", "instructions": SCOLD_Q}
    return {"state": state, "questions": qs}


def call(body, key):
    req = urllib.request.Request(URL, data=json.dumps(body, ensure_ascii=False).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "ai-model-id": MODEL,
        "ai-evaluation-model-specification-version": "4",
        "ai-gateway-protocol-version": "0.0.1",
        "ai-gateway-auth-method": "api-key",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def threshold(rule):
    spec = os.environ.get("LIMPET_BLOCK", "").strip()
    if not spec:
        return float("inf")
    try:
        return float(spec)
    except ValueError:
        pass
    for part in spec.split(","):
        k, _, v = part.partition("=")
        if k.strip() and k.strip() in rule:
            return float(v)
    return float("inf")


# --- entry points ---

def main():
    hook = json.load(sys.stdin)
    if hook.get("stop_hook_active"):  # we already pushed back once on this stop; don't loop
        return 0
    key = api_key()
    if not key:
        return 0
    rules = load_rules()
    context, last, tools = read_tail(hook["transcript_path"])
    if not last:
        return 0
    t0 = time.time()
    try:
        res = call(build_request(rules, context, last, tools), key)
    except Exception as e:  # noqa: BLE001  never block on API trouble
        res = {"error": str(e)[:300]}
    ans = res.get("answers", {})
    probs = {r: ans.get(f"r{i}", {}).get("probability") for i, r in enumerate(rules)}
    probs["(scold)"] = ans.get("scold", {}).get("probability")
    hits = [(r, p) for r, p in probs.items() if p is not None and p >= threshold(r)]
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": hook.get("session_id"), "cwd": hook.get("cwd"),
           "transcript_path": hook.get("transcript_path"), "ms": int((time.time() - t0) * 1000),
           "usage": res.get("usage"), "error": res.get("error"), "probs": probs,
           "blocked": [r for r, _ in hits], "n_tools": len(tools), "assistant_text": last[:500]}
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if hits:
        print("limpet: this response may violate: " + "; ".join(f'"{r}" ({p:.0%})' for r, p in hits)
              + ". If it does, follow the rule and keep working. If it does not, say why in one line, then stop.",
              file=sys.stderr)  # exit 2 + stderr is the form Claude Code reliably acts on
        return 2
    return 0


def stats(path=LOG):
    rows = []
    with open(path, encoding="utf-8") as f:
        for l in f:
            try:
                rows.append(json.loads(l))
            except ValueError:
                continue
    print(f"{len(rows)} stops in {path}")
    for r in sorted({r for d in rows for r in d.get("probs", {})}):
        ps = sorted(p for d in rows if (p := d["probs"].get(r)) is not None)
        if not ps:
            continue
        q = lambda f: ps[min(len(ps) - 1, int(f * len(ps)))]  # noqa: E731
        blocked = sum(r in d.get("blocked", []) for d in rows)
        print(f"p50={q(.5):.2f} p90={q(.9):.2f} p95={q(.95):.2f} max={ps[-1]:.2f} blocked={blocked:3d}  {r}")


if __name__ == "__main__":
    if "--stats" in sys.argv[1:]:
        stats()
    else:
        sys.exit(main())
