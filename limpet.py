#!/usr/bin/env python3
"""limpet: a Stop hook that stops your coding agent from stopping too early.

Works as a Stop hook in Claude Code and Codex CLI. When the agent is about to stop, limpet sends the last
few turns, this turn's tool calls, and the final message to jev (TypeSafe AI's evaluation model, directly or
via Vercel AI Gateway). jev returns, for every rule in rules.md, the probability that the rule was just
violated. If a rule is over its threshold, the hook exits 2 with a message telling the agent to keep working.

    echo '{"transcript_path": "...", "stop_hook_active": false}' | python3 limpet.py
    python3 limpet.py suggest      # mine your transcripts for the rules you actually need
    python3 limpet.py calibrate    # score rules.md against your transcripts and print LIMPET_BLOCK

Configuration, in order of precedence: environment, Claude Code plugin config (CLAUDE_PLUGIN_OPTION_*),
then ~/.limpet/env (KEY=VALUE lines).
    TYPESAFE_API_KEY    TypeSafe key (https://console.typesafe.ai/keys) => calls api.typesafe.ai directly.
    AI_GATEWAY_API_KEY  Vercel AI Gateway key => calls jev through the gateway. Either key is enough.
    LIMPET_KEY_CMD      a shell command that prints the key, if you keep it in a password manager.
    LIMPET_PROVIDER     "typesafe" or "vercel". Only needed with LIMPET_KEY_CMD (default vercel).
    LIMPET_BLOCK        "0.8" (one threshold for all rules) or "run the tests=0.5,hand work=0.6"
                        (a substring of the rule text => its threshold). Unset = shadow mode: log, never block.
    LIMPET_RULES        rules file (default: ~/.limpet/rules.md, created from the bundled rules.md on first run)
    LIMPET_LOG          log file (default: ~/.limpet/log.jsonl)
    LIMPET_JEV_MODEL    default jev-latest (TypeSafe) / typesafe-ai/jev (Vercel)

No key, API down, timeout, or any other error => exit 0 silently. A hook must never stop the work.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~/.limpet")
PROVIDERS = {  # url, model, yes/no question type, answer field
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "jev-latest", "noul", "noul"),
    "vercel": ("https://ai-gateway.vercel.sh/v4/ai/evaluation-model", "typesafe-ai/jev", "boolean", "probability"),
}
TIMEOUT = 20  # jev call; the key command gets 8 s, so both fit in the hook's 30 s
TAIL_BYTES = 512 * 1024  # only the tail of the transcript is read
N_CONTEXT = 3  # previous messages sent as context
MAXLEN = 2000  # per message
MAX_TOOLS = 40  # tool calls of the current turn, most recent kept

# user lines that look human but are injected by the harness (collected from real transcripts)
NOT_HUMAN_PREFIXES = (
    "<teammate-message", "<system-reminder", "<command-name>", "<command-message", "<local-command-caveat",
    "<local-command-stdout", "<user-prompt-submit-hook", "Another Claude session sent",
    "The coordinator sent a message", "[SYSTEM NOTIFICATION", "[Request interrupted", "[Image:",
    "Stop hook feedback:", "Caveat: The messages below", "API Error",
    "The previous response failed to produce a valid tool call",
    "# AGENTS.md instructions", "<environment_context", "<user_instructions", "<permissions instructions",
    "The following is the Codex agent history",  # Codex approval automations
)


# --- config ---

def _plugin_option(name):
    return os.environ.get("CLAUDE_PLUGIN_OPTION_" + (name[7:] if name.startswith("LIMPET_") else name))


def load_env(path=os.path.join(HOME, "env")):
    """KEY=VALUE lines. Environment and plugin config win; the file fills in what is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            for l in f:
                k, eq, v = l.strip().partition("=")
                if eq and k and not k.startswith("#") and not os.environ.get(k) and not _plugin_option(k):
                    os.environ[k] = v.strip().strip("'\"")
    except OSError:
        pass


def cfg(name, default=None):
    return os.environ.get(name) or _plugin_option(name) or default


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
            k = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            k = None
        if k:
            return cfg("LIMPET_PROVIDER", "vercel"), k
    return None, None


def threshold(rule):
    """LIMPET_BLOCK => threshold for this rule. Malformed parts are ignored, never fatal."""
    spec = (cfg("LIMPET_BLOCK") or "").strip()
    if not spec:
        return float("inf")
    try:
        return float(spec)
    except ValueError:
        pass
    for part in spec.split(","):
        k, _, v = part.rpartition("=")
        try:
            if k.strip() and k.strip() in rule:
                return float(v)
        except ValueError:
            continue
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
    inp = inp if isinstance(inp, dict) else {}
    v = inp.get("command") or inp.get("cmd") or inp.get("file_path") or inp.get("path") or inp.get("pattern") \
        or inp.get("url") or next((x for x in inp.values() if isinstance(x, str)), "")
    s = f"{name}: {v}".strip()
    return s if len(s) <= 200 else s[:200] + "…"


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


def _rows(text):
    rows = []
    for l in text.splitlines():
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    return rows


def read_tail(path):
    """(previous messages newest first, final assistant message, tool calls of this turn)."""
    with open(path, "rb") as f:
        start = max(0, os.path.getsize(path) - TAIL_BYTES)
        f.seek(start)
        text = f.read().decode("utf-8", "replace")
    if start:  # the first line is cut mid-way
        text = text.split("\n", 1)[-1]
    rows = _rows(text)
    texts = [s for d in rows for s in [assistant_text(d) or human_text(d)] if s]
    if not texts:
        return [], None, []
    *ctx, last = texts
    return [c[:MAXLEN] for c in ctx[::-1][:N_CONTEXT]], last[:MAXLEN], tools_since_turn(rows)


# --- jev ---

def _state(context, last, tools):
    return {"previous messages (newest first)": context, "tool calls this turn (oldest first)": tools or [],
            "agent's final message before stopping": last}


def rule_questions(rules, provider):
    qtype = PROVIDERS[provider][2]
    return {f"r{i}": {"type": qtype, "instructions": f"Is the agent violating this rule? Rule: {r}"} for i, r in enumerate(rules)}


def call(state, questions, key, provider):
    url, model, _, _ = PROVIDERS[provider]
    body = {"state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "typesafe":
        body["model"] = cfg("LIMPET_JEV_MODEL", model)
    else:
        headers.update({"ai-model-id": cfg("LIMPET_JEV_MODEL", model), "ai-evaluation-model-specification-version": "4",
                        "ai-gateway-protocol-version": "0.0.1", "ai-gateway-auth-method": "api-key"})
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def prob(answer, provider):
    p = (answer or {}).get(PROVIDERS[provider][3])
    return float(p) if isinstance(p, (int, float)) else None


def choice(answer):
    return answer.get("choice") or max(answer["probabilities"], key=answer["probabilities"].get), answer.get("confidence", 0)


# --- the hook ---

def main():
    hook = json.load(sys.stdin)
    if hook.get("stop_hook_active"):  # Claude Code and Codex both set this after a push-back; don't loop
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
        res = call(_state(context, last, tools), rule_questions(rules, provider), key, provider)
        ans = res.get("answers") or {}
    except Exception as e:  # noqa: BLE001  never block on API trouble
        res, ans = {"error": str(e)[:300]}, {}
    probs = {r: prob(ans.get(f"r{i}"), provider) for i, r in enumerate(rules)}
    hits = [(r, p) for r, p in probs.items() if p is not None and p >= threshold(r)]
    log = os.path.expanduser(cfg("LIMPET_LOG", os.path.join(HOME, "log.jsonl")))
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": "codex" if "turn_id" in hook else "claude", "provider": provider,
           "session_id": hook.get("session_id"), "cwd": hook.get("cwd"), "transcript_path": hook.get("transcript_path"),
           "ms": int((time.time() - t0) * 1000), "usage": res.get("usage"), "error": res.get("error"), "probs": probs,
           "blocked": [r for r, _ in hits], "n_tools": len(tools), "assistant_text": last[:500]}
    if os.path.dirname(log):
        os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if hits:
        print("limpet: this response may violate: " + "; ".join(f'"{r}" ({p:.0%})' for r, p in hits)
              + ". If it does, follow the rule and keep working. If it does not, say why in one line, then stop.",
              file=sys.stderr)  # exit 2 + stderr: the form both Claude Code and Codex act on
        return 2
    return 0


# --- suggest and calibrate: learn from your own transcripts ---

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
# failure type => indices into the bundled rules.md that target it at stop time
CATCHABLE = {"handoff": [4, 5, 6, 7], "overreach": [8, 1], "misread": [3], "taste": [9]}
NOT_CATCHABLE = "not visible at stop time (the failure only shows up later, or needs context the transcript doesn't have)"


def transcript_files(days):
    cutoff = time.time() - days * 86400
    paths = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")) + glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl"))
    return [p for p in paths if os.path.getmtime(p) >= cutoff]


def scan(path):
    """Every (agent stop, what the human said next) pair in one transcript, with context and tools."""
    with open(path, encoding="utf-8", errors="replace") as f:
        rows = _rows(f.read())
    texts = []  # (row index, "a"|"h", text)
    for i, d in enumerate(rows):
        a = assistant_text(d)
        h = None if a else human_text(d)
        if a or h:
            texts.append((i, "a" if a else "h", a or h))
    out = []
    for j in range(len(texts) - 1):
        i, kind, last = texts[j]
        _, nkind, nxt = texts[j + 1]
        if kind != "a" or nkind != "h":
            continue
        ctx = [t for _, _, t in texts[max(0, j - N_CONTEXT):j]][::-1]
        out.append({"file": path, "context": [c[:MAXLEN] for c in ctx], "tools": tools_since_turn(rows[:i + 1]),
                    "last": last[:MAXLEN], "next": nxt[:1500], "ts": rows[i].get("timestamp") or rows[i].get("ts") or ""})
    return out


def past_stops(days, limit):
    files = transcript_files(days)
    stops = [s for p in files for s in scan(p)]
    seen = {}
    for s in stops:  # a long reply repeated verbatim is a template from an automation, not a human reacting
        seen[s["next"][:200]] = seen.get(s["next"][:200], 0) + 1
    stops = [s for s in stops if len(s["next"]) <= 80 or seen[s["next"][:200]] < 5]
    return files, sorted(stops, key=lambda s: s["ts"], reverse=True)[:limit]


def reaction_question(stop):
    """The human's reply goes into the question, not the state, so rule questions in the same call can't see it."""
    return {"type": "choice", "criteria": REACTIONS,
            "instructions": f"How did the human react to the agent's final message? The human replied: {stop['next']}"}


def ask_all(fn, stops):
    with ThreadPoolExecutor(8) as ex:
        return [(s, r) for s, r in zip(stops, ex.map(fn, stops)) if "error" not in r]


def _safe(fn):
    def wrapped(stop):
        try:
            return fn(stop)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}
    return wrapped


def _oneline(s, n=110):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def suggest(days=30, limit=2000):
    load_env()
    provider, key = api_key()
    if not key:
        print("limpet suggest needs a jev key (TYPESAFE_API_KEY or AI_GATEWAY_API_KEY).", file=sys.stderr)
        return 1
    files, stops = past_stops(days, limit)
    print(f"{len(stops)} stops with a human reply, from {len(files)} transcripts (last {days} days). Asking jev...", file=sys.stderr)
    t0 = time.time()

    def classify(stop):
        qs = {"reaction": reaction_question(stop),
              "failure": {"type": "choice", "criteria": FAILURES,
                          "instructions": f"If the human was unhappy, what did the agent do wrong? The human replied: {stop['next']}"}}
        ans = call(_state(stop["context"], stop["last"], stop["tools"]), qs, key, provider)["answers"]
        return {q: choice(ans[q]) for q in qs}

    ok = ask_all(_safe(classify), stops)
    bad = [(s, r) for s, r in ok if r["reaction"][0] in ("push", "correction")]
    bundled = load_rules(os.path.join(HERE, "rules.md"))
    lines = ["# limpet suggest", "",
             f"{len(ok)} stops from {len(files)} transcripts, last {days} days, {int(time.time() - t0)} s of jev"
             + (f", {len(stops) - len(ok)} errors" if len(stops) > len(ok) else "") + ".", "",
             "## How the human reacted to the agent stopping", ""]
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
            lines += [f"- {bundled[i]}" for i in CATCHABLE[k] if i < len(bundled)]
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


def auroc(pos, neg):
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def block_key(rule, rules):
    """Shortest prefix of the rule that no other rule contains and that LIMPET_BLOCK can parse (no ',' or '=')."""
    for n in range(6, len(rule) + 1):
        k = rule[:n]
        if "," in k or "=" in k:
            break
        if not any(k in r for r in rules if r != rule):
            return k
    return re.sub(r"[,=].*", "", rule)[:30]


def calibrate(days=30, limit=2000, fp=0.05):
    load_env()
    provider, key = api_key()
    if not key:
        print("limpet calibrate needs a jev key (TYPESAFE_API_KEY or AI_GATEWAY_API_KEY).", file=sys.stderr)
        return 1
    rules = load_rules()
    files, stops = past_stops(days, limit)
    print(f"{len(stops)} stops from {len(files)} transcripts (last {days} days), {len(rules)} rules. Asking jev...", file=sys.stderr)
    t0 = time.time()

    def score(stop):
        qs = {**rule_questions(rules, provider), "reaction": reaction_question(stop)}
        ans = call(_state(stop["context"], stop["last"], stop["tools"]), qs, key, provider)["answers"]
        return {"probs": [prob(ans.get(f"r{i}"), provider) for i in range(len(rules))],
                "bad": choice(ans["reaction"])[0] in ("push", "correction")}

    results = [r for _, r in ask_all(_safe(score), stops)]
    bad = [r for r in results if r["bad"]]
    good = [r for r in results if not r["bad"]]
    print(f"{len(results)} scored in {int(time.time() - t0)} s: {len(bad)} stops the human pushed back on, {len(good)} fine.\n")
    print(f"{'AUROC':>6} {'thr':>5} {'catches':>8} {'blocks':>7}  rule")
    spec = []
    for i, rule in enumerate(rules):
        pos = [r["probs"][i] for r in bad if r["probs"][i] is not None]
        neg = sorted(r["probs"][i] for r in good if r["probs"][i] is not None)
        if not pos or not neg:
            continue
        a = auroc(pos, neg)
        thr = neg[min(len(neg) - 1, int((1 - fp) * len(neg)))]
        catch = sum(p >= thr for p in pos) / len(pos)
        blocks = sum(n >= thr for n in neg) / len(neg)  # can exceed fp when many fine stops tie at thr
        mark = "" if a >= 0.55 else "   (does not separate; left in shadow)"
        print(f"{a:6.2f} {thr:5.2f} {catch:7.0%} {blocks:7.0%}  {rule[:70]}{mark}")
        if a >= 0.55:
            spec.append(f"{block_key(rule, rules)}={thr:.2f}")
    print(f"\nthr = the value that blocks about {fp:.0%} of your fine stops; catches = share of bad stops at or above it; "
          "blocks = share of fine stops actually at or above it.")
    if spec:
        print(f"\nLIMPET_BLOCK=\"{','.join(spec)}\"")
    else:
        print("\nNo rule separates your bad stops from your fine ones yet. Rewrite the rules to describe what the agent says, not what it did wrong.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="limpet: a Stop hook backed by jev. With no subcommand, runs as the hook (JSON on stdin).")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("suggest", "calibrate"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=30)
        p.add_argument("--max", type=int, default=2000)
        if name == "calibrate":
            p.add_argument("--fp", type=float, default=0.05, help="accepted share of fine stops to block")
    a = ap.parse_args()
    if a.cmd == "suggest":
        sys.exit(suggest(a.days, a.max))
    if a.cmd == "calibrate":
        sys.exit(calibrate(a.days, a.max, a.fp))
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001  a hook must never stop the work
        sys.exit(0)
