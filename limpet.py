#!/usr/bin/env python3
"""limpet: a Stop hook that stops your coding agent from stopping too early.

Works as a Stop hook in Claude Code and Codex CLI. When the agent is about to stop, limpet sends the last
few turns, this turn's tool calls, and the final message to jev (TypeSafe AI's evaluation model, via Vercel
AI Gateway). jev returns, for every rule in rules.md, the probability that the rule was just violated. If a
rule is over its threshold, the hook exits 2 with a message telling the agent to keep working.

    echo '{"transcript_path": "...", "stop_hook_active": false}' | python3 limpet.py
    python3 limpet.py --stats      # per-rule percentiles from the log, to pick thresholds
    python3 limpet.py suggest      # mine your transcripts for the rules you actually need

Configuration, in order of precedence: environment, then ~/.limpet/env (KEY=VALUE lines).
    TYPESAFE_API_KEY    TypeSafe key (https://console.typesafe.ai/keys) => calls api.typesafe.ai directly.
    AI_GATEWAY_API_KEY  Vercel AI Gateway key => calls jev through the gateway. Either key is enough.
    LIMPET_KEY_CMD      a shell command that prints the key, if you keep it in a password manager.
    LIMPET_PROVIDER     "typesafe" or "vercel". Only needed with LIMPET_KEY_CMD (default vercel).
    LIMPET_BLOCK        "0.8" (one threshold for all rules) or "run the tests=0.5,hand work=0.6"
                        (a substring of the rule text => its threshold). Unset = shadow mode: log, never block.
    LIMPET_RULES        rules file (default: ~/.limpet/rules.md, created from the bundled rules.md on first run)
    LIMPET_LOG          log file (default: ~/.limpet/log.jsonl)
    LIMPET_JEV_MODEL    default typesafe-ai/jev
When installed as a Claude Code plugin, the key and thresholds also come from the plugin's user config
(CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY, CLAUDE_PLUGIN_OPTION_AI_GATEWAY_API_KEY, CLAUDE_PLUGIN_OPTION_BLOCK).

No key, API down, or timeout => exit 0 silently. A hook must never stop the work.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~/.limpet")
PROVIDERS = {  # url, model, question type, answer field
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "jev-latest", "noul", "noul"),
    "vercel": ("https://ai-gateway.vercel.sh/v4/ai/evaluation-model", "typesafe-ai/jev", "boolean", "probability"),
}
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
    "# AGENTS.md instructions", "<environment_context", "<user_instructions", "<permissions instructions",
)


# --- config ---

def load_env(path=os.path.join(HOME, "env")):
    """KEY=VALUE lines. Environment wins; the file fills in what is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            for l in f:
                k, eq, v = l.strip().partition("=")
                if eq and k and not k.startswith("#") and not os.environ.get(k):
                    os.environ[k] = v.strip().strip("'\"")
    except OSError:
        pass


def cfg(name, default=None):
    return os.environ.get(name) or os.environ.get("CLAUDE_PLUGIN_OPTION_" + name.removeprefix("LIMPET_")) or default


def rules_path():
    p = cfg("LIMPET_RULES")
    if p:
        return os.path.expanduser(p)
    mine = os.path.join(HOME, "rules.md")
    if not os.path.exists(mine):  # first run: give the user their own copy to edit
        os.makedirs(HOME, exist_ok=True)
        shutil.copy(os.path.join(HERE, "rules.md"), mine)
    return mine


def load_rules(path=None):
    with open(path or rules_path(), encoding="utf-8") as f:
        return [l[2:].strip() for l in f if l.startswith("- ")]


def api_key():
    """(provider, key) or (None, None). A TypeSafe key wins over a gateway key; LIMPET_KEY_CMD is the fallback."""
    if cfg("TYPESAFE_API_KEY"):
        return "typesafe", cfg("TYPESAFE_API_KEY")
    if cfg("AI_GATEWAY_API_KEY"):
        return "vercel", cfg("AI_GATEWAY_API_KEY")
    cmd = cfg("LIMPET_KEY_CMD")
    if cmd:
        try:
            k = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=25).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            k = None
        if k:
            return cfg("LIMPET_PROVIDER", "vercel"), k
    return None, None


def threshold(rule):
    spec = (cfg("LIMPET_BLOCK") or "").strip()
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


# --- transcripts (Claude Code and Codex CLI) ---

def _clean_human(s):
    s = (s or "").strip()
    if not s or s.startswith(NOT_HUMAN_PREFIXES) or re.match(r"<[a-z][a-z_-]*[ >]", s):
        return None
    return s


def human_text(d):
    """Text the human typed, or None for tool results and harness-injected lines."""
    if d.get("type") == "response_item":  # Codex
        p = d.get("payload") or {}
        if p.get("type") != "message" or p.get("role") != "user":
            return None
        return _clean_human("\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)))
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return _clean_human(c) if isinstance(c, str) else None


def assistant_text(d):
    if d.get("type") == "response_item":  # Codex
        p = d.get("payload") or {}
        if p.get("type") != "message" or p.get("role") != "assistant":
            return None
        c = p.get("content") or []
    elif d.get("type") == "assistant":
        c = (d.get("message") or {}).get("content")
    else:
        return None
    if not isinstance(c, list):
        return None
    s = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") in ("text", "output_text")).strip()
    return s or None


def tool_line(name, inp):
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"input": inp}
    inp = inp or {}
    v = inp.get("command") or inp.get("cmd") or inp.get("file_path") or inp.get("path") or inp.get("pattern") \
        or inp.get("url") or next((x for x in inp.values() if isinstance(x, str)), "")
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
        if d.get("type") == "response_item":  # Codex
            p = d.get("payload") or {}
            if p.get("type") in ("function_call", "custom_tool_call"):
                uses.append((p.get("call_id"), tool_line(p.get("name"), p.get("arguments") or p.get("input"))))
            elif p.get("type") in ("function_call_output", "custom_tool_call_output"):
                results[p.get("call_id")] = "done"
            continue
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

def build_request(rules, context, last, tools=None, provider="vercel"):
    url, model, qtype, _ = PROVIDERS[provider]
    state = {"previous messages (newest first)": context, "tool calls this turn (oldest first)": tools or [],
             "agent's final message before stopping": last}
    qs = {f"r{i}": {"type": qtype, "instructions": f"Is the agent violating this rule? Rule: {r}"}
          for i, r in enumerate(rules)}
    qs["scold"] = {"type": qtype, "instructions": SCOLD_Q}
    body = {"state": state, "questions": qs}
    if provider == "typesafe":
        body["model"] = cfg("LIMPET_JEV_MODEL", model)
    return body


def call(body, key, provider="vercel"):
    url, model, _, _ = PROVIDERS[provider]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "vercel":
        headers.update({"ai-model-id": cfg("LIMPET_JEV_MODEL", model), "ai-evaluation-model-specification-version": "4",
                        "ai-gateway-protocol-version": "0.0.1", "ai-gateway-auth-method": "api-key"})
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def prob(answer, provider):
    return (answer or {}).get(PROVIDERS[provider][3])


# --- entry points ---

def main():
    hook = json.load(sys.stdin)
    if hook.get("stop_hook_active"):  # we already pushed back once on this stop; don't loop
        return 0
    load_env()
    provider, key = api_key()
    if not key:
        return 0
    rules = load_rules()
    context, last, tools = [], None, []
    if hook.get("transcript_path") and os.path.exists(hook["transcript_path"]):
        context, last, tools = read_tail(hook["transcript_path"])
    last = (hook.get("last_assistant_message") or last or "")[:MAXLEN]
    if not last:
        return 0
    t0 = time.time()
    try:
        res = call(build_request(rules, context, last, tools, provider), key, provider)
    except Exception as e:  # noqa: BLE001  never block on API trouble
        res = {"error": str(e)[:300]}
    ans = res.get("answers", {})
    probs = {r: prob(ans.get(f"r{i}"), provider) for i, r in enumerate(rules)}
    probs["(scold)"] = prob(ans.get("scold"), provider)
    hits = [(r, p) for r, p in probs.items() if p is not None and p >= threshold(r)]
    log = os.path.expanduser(cfg("LIMPET_LOG", os.path.join(HOME, "log.jsonl")))
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": "codex" if "turn_id" in hook else "claude", "provider": provider,
           "session_id": hook.get("session_id"), "cwd": hook.get("cwd"), "transcript_path": hook.get("transcript_path"),
           "ms": int((time.time() - t0) * 1000), "usage": res.get("usage"), "error": res.get("error"), "probs": probs,
           "blocked": [r for r, _ in hits], "n_tools": len(tools), "assistant_text": last[:500]}
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if hits:
        print("limpet: this response may violate: " + "; ".join(f'"{r}" ({p:.0%})' for r, p in hits)
              + ". If it does, follow the rule and keep working. If it does not, say why in one line, then stop.",
              file=sys.stderr)  # exit 2 + stderr: the form both Claude Code and Codex act on
        return 2
    return 0


def stats(path=None):
    load_env()
    path = os.path.expanduser(path or cfg("LIMPET_LOG", os.path.join(HOME, "log.jsonl")))
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


# --- suggest: mine transcripts for rules ---

REACTIONS = {
    "push": "the human tells the agent to do what it should already have done: 'do it', 'continue', 'go ahead', 'fix it', 'やって', '進めて'",
    "correction": "the human points out a mistake, disagrees, or shows frustration: 'no', 'wrong', 'that's not it', 'why did you', '違う'",
    "question": "the human asks a question about the work",
    "new_request": "the human moves on to a new or follow-up task, satisfied with this one",
    "ack": "the human acknowledges, thanks, or says ok",
}
FAILURES = {
    "handoff": "the agent stopped and handed the work back: waiting, asking permission, offering options, telling the human to do it",
    "unverified": "the agent reported success, completion, or 'all green' that later turned out to be false",
    "misread": "the agent did something other than what was asked, e.g. implemented when only a proposal was requested",
    "overreach": "the agent did or proposed things nobody asked for, used too many resources, or decided on its own",
    "bug": "the fix itself was wrong, broke something, or did not fix the problem",
    "wrong_target": "the agent worked on the wrong file, repo, destination, item, or model",
    "stale": "the agent concluded from stale state without pulling, refetching, or rechecking",
    "taste": "quality or preference: too thin, too short, wrong style, wrong wording, wrong language",
    "other": "none of the above",
}
# what limpet can act on at stop time, and the rule that targets it
CATCHABLE = {
    "handoff": ["Don't ask \"shall I start?\" or \"I'll run it if that's OK\" for work that was already requested. Only ask before irreversible actions: production databases, deletion, sending to external services, payments, merges",
                "Don't hand work to the human unless only a human can do it (biometric auth, payments, physical actions)",
                "Fix problems you find before stopping. Don't stop at \"CI is failing\" or \"there's a bug\". If you can't fix it, say why",
                "When waiting, give a time estimate. Don't stop with \"I'll wait for it to finish\""],
    "misread": ["Don't confuse a request for a proposal or advice with a request to act. If asked for a proposal, don't implement it"],
    "overreach": ["Don't stop to offer things that weren't asked for (\"want me to automate this too?\"). Proposals go in the last line of the report, at most",
                  "Don't edit files outside the scope of the task"],
    "taste": ["Answer in the language the human writes in"],
}
NOT_CATCHABLE = "not visible at stop time (the failure only shows up later, or needs context the transcript doesn't have)"


def transcript_files(days):
    cutoff = time.time() - days * 86400
    paths = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")) + glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl"))
    return [p for p in paths if os.path.getmtime(p) >= cutoff]


def scan(path):
    """Every (agent stop, what the human said next) pair in one transcript, with context and tools."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for l in f:
            try:
                rows.append(json.loads(l))
            except ValueError:
                continue
    texts = []  # (index, "a"|"h", text)
    for i, d in enumerate(rows):
        a = assistant_text(d)
        h = None if a else human_text(d)
        if a or h:
            texts.append((i, "a" if a else "h", a or h))
    out, seen = [], set()
    for j in range(len(texts) - 1):
        i, kind, last = texts[j]
        ni, nkind, nxt = texts[j + 1]
        if kind != "a" or nkind != "h":
            continue
        key = (nxt[:200])  # parallel agents get the same reply attached to several stops; keep the last one
        if key in seen:
            continue
        seen.add(key)
        ctx = [t for _, _, t in texts[max(0, j - N_CONTEXT):j]][::-1]
        out.append({"file": path, "context": [c[:MAXLEN] for c in ctx], "tools": tools_since_turn(rows[:i + 1]),
                    "last": last[:MAXLEN], "next": nxt[:1500], "ts": rows[i].get("timestamp") or rows[i].get("ts") or ""})
    return out


def classify(stop, key, provider):
    url, model, _, _ = PROVIDERS[provider]
    state = {"previous messages (newest first)": stop["context"], "tool calls this turn (oldest first)": stop["tools"],
             "agent's final message before stopping": stop["last"], "what the human said next": stop["next"]}
    qs = {"reaction": {"type": "choice", "instructions": "How did the human react to the agent's final message?", "criteria": REACTIONS},
          "failure": {"type": "choice", "instructions": "If the human was unhappy, what did the agent do wrong?", "criteria": FAILURES}}
    body = {"state": state, "questions": qs}  # both providers take choice questions with "criteria"
    if provider == "typesafe":
        body["model"] = cfg("LIMPET_JEV_MODEL", model)
    try:
        ans = call(body, key, provider)["answers"]
        return {q: (ans[q].get("choice") or max(ans[q]["probabilities"], key=ans[q]["probabilities"].get),
                    ans[q].get("confidence", 0)) for q in qs}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def suggest(days=30, limit=2000):
    from concurrent.futures import ThreadPoolExecutor
    load_env()
    provider, key = api_key()
    if not key:
        print("limpet suggest needs a jev key (TYPESAFE_API_KEY or AI_GATEWAY_API_KEY).", file=sys.stderr)
        return 1
    files = transcript_files(days)
    stops = [s for p in files for s in scan(p)]
    stops.sort(key=lambda s: s["ts"], reverse=True)
    stops = stops[:limit]
    print(f"{len(stops)} stops with a human reply, from {len(files)} transcripts (last {days} days). Asking jev...", file=sys.stderr)
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda s: classify(s, key, provider), stops))
    ok = [(s, r) for s, r in zip(stops, results) if "error" not in r]
    errors = len(stops) - len(ok)
    bad = [(s, r) for s, r in ok if r["reaction"][0] in ("push", "correction")]
    lines = [f"# limpet suggest", "",
             f"{len(ok)} stops from {len(files)} transcripts, last {days} days, {int(time.time() - t0)} s of jev"
             + (f", {errors} errors" if errors else "") + ".", "", "## How the human reacted to the agent stopping", ""]
    counts = {k: sum(r["reaction"][0] == k for _, r in ok) for k in REACTIONS}
    for k, n in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"- {k:<12} {n:5}  {n / max(1, len(ok)):4.0%}")
    lines += ["", f"## What went wrong ({len(bad)} stops the human pushed back on)", ""]
    by = {k: [(s, r) for s, r in bad if r["failure"][0] == k] for k in FAILURES}
    for k, items in sorted(by.items(), key=lambda x: -len(x[1])):
        if not items:
            continue
        tag = ("only the wrong-language part is visible at stop time" if k == "taste"
               else "limpet can target this at stop time" if k in CATCHABLE else NOT_CATCHABLE)
        lines.append(f"### {k}: {len(items)} ({len(items) / max(1, len(bad)):.0%}) — {tag}")
        for s, r in sorted(items, key=lambda x: -x[1]["failure"][1])[:3]:
            lines.append(f"- agent: {_oneline(s['last'])}")
            lines.append(f"  human: {_oneline(s['next'])}")
        lines.append("")
    lines += ["## Suggested rules", "", "Ordered by how much of your pain each one targets. Copy the ones you want into ~/.limpet/rules.md.", ""]
    for k, items in sorted(by.items(), key=lambda x: -len(x[1])):
        if k in CATCHABLE and items:
            lines.append(f"<!-- {k}: {len(items) / max(1, len(bad)):.0%} of your bad stops -->")
            lines += [f"- {rule}" for rule in CATCHABLE[k]]
    untargetable = sum(len(v) for k, v in by.items() if k not in CATCHABLE)
    lines += ["", f"{untargetable / max(1, len(bad)):.0%} of your bad stops are types no stop-time rule can see. "
              "Those need verification (run the tests, check CI) rather than a rule.", ""]
    out = os.path.join(HOME, "suggest.md")
    os.makedirs(HOME, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"written to {out}", file=sys.stderr)
    return 0


def _oneline(s, n=110):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--stats" in args:
        stats()
    elif args and args[0] == "suggest":
        days = int(args[args.index("--days") + 1]) if "--days" in args else 30
        limit = int(args[args.index("--max") + 1]) if "--max" in args else 2000
        sys.exit(suggest(days, limit))
    else:
        sys.exit(main())
