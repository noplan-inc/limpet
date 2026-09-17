<p align="center">
  <img src="assets/logo.svg" width="160" alt="limpet">
</p>

<h1 align="center">limpet</h1>

<p align="center">
  <em>Your agent stops. limpet doesn't let it.</em>
</p>

<p align="center">
  <a href="https://github.com/noplan-inc/limpet/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/noplan-inc/limpet/test.yml?style=flat-square&label=tests&color=0b6e6e" alt="tests"></a>
  <img src="https://img.shields.io/badge/works%20with-Claude%20Code%20%C2%B7%20Codex-0b6e6e?style=flat-square" alt="Claude Code and Codex">
  <img src="https://img.shields.io/badge/deps-stdlib%20only-0b6e6e?style=flat-square" alt="stdlib only">
  <img src="https://img.shields.io/badge/per%20stop-0.7s%20%C2%B7%20%240.0001-0b6e6e?style=flat-square" alt="0.7 s and a hundredth of a cent per stop">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0b6e6e?style=flat-square" alt="MIT"></a>
</p>

<p align="center">
  <sub>English &middot; <a href="README.ja.md">日本語</a></sub>
</p>

---

Coding agents stop early. *"I'll wait for CI."* *"Shall I run it?"* *"Please ask the team about this."* You type **do it**. Then again. Then again.

limpet is a Stop hook. You write rules in plain language. Every time the agent is about to stop, [jev](https://typesafe.ai) scores the stop against every rule in **0.7 seconds**, and if a rule is violated the agent is sent back to work instead of stopping. It clings to the task like a limpet to a rock.

One Python file. Standard library only. No regexes to maintain, no local model, no training.

## Before / after

Without limpet:

```
● I've found the bug in auth.py. The fix is a one-line change to the
  token check. Want me to apply it?

> do it
```

With limpet, the same stop never reaches you:

```
● I've found the bug in auth.py. The fix is a one-line change to the
  token check. Want me to apply it?

  ⏹ limpet: this response may violate: "Don't ask 'shall I start?' for
    work that was already requested" (91%). If it does, follow the rule
    and keep working. If it does not, say why in one line, then stop.

● Applying the fix.
  ⎿ Edit auth.py
  ⎿ Bash pytest -q · 42 passed
  Done. The token check now rejects expired tokens; tests pass.
```

## How it works

```
agent is about to stop
        │
        ▼
limpet reads the last 3 messages + this turn's tool calls + the final message
        │
        ▼
jev answers one yes/no question per rule, all in parallel, in ~0.7 s
   "Don't say done without running tests"        →  0.08
   "Don't ask 'shall I start?' for requested work" →  0.91  ◀ over threshold
   "Fix problems you find before stopping"        →  0.31
        │
        ▼
exit 2 + one line on stderr → the agent keeps working
```

The second stop of the same chain is always allowed through, so the agent is pushed back at most once per stop. If it disagrees, it says why in one line and stops. False positives cost one sentence. Missed early stops cost you a round trip. Tune for recall.

## Install

You need a key for jev. Either one works:

- **TypeSafe** (direct): https://console.typesafe.ai/keys
- **Vercel AI Gateway**: https://vercel.com/ai-gateway, model `typesafe-ai/jev`

A stop is one call with one yes/no question per rule, 1,000 to 2,000 input tokens. At jev's price that is about a hundredth of a cent, so a heavy day is a few cents.

### Claude Code

```
/plugin marketplace add noplan-inc/limpet
```
```
/plugin install limpet@limpet
```

Claude Code asks for your key and thresholds on install (they go to secure storage, not to `settings.json`). Or from the shell:

```sh
claude plugin marketplace add noplan-inc/limpet
claude plugin install limpet@limpet --config typesafe_api_key=... --config block=0.8
```

### Codex

```sh
codex plugin marketplace add noplan-inc/limpet
codex plugin add limpet@limpet
```

Then run `codex`, open `/hooks`, and trust limpet's Stop hook. Codex plugins don't carry secrets, so store the key in the OS keychain (macOS Keychain, or libsecret on Linux). It prompts for the key and nothing is written to disk in plain text:

```sh
python3 ~/.codex/plugins/cache/limpet/limpet/0.1.0/limpet.py key set
```

### Requirements

Python 3.9 or newer on `PATH` as `python3`. Tested on macOS and Linux.

### Any agent with Claude-style hooks

```sh
git clone https://github.com/noplan-inc/limpet ~/limpet
```

Add a `Stop` hook running `python3 ~/limpet/limpet.py` (timeout 30) to `~/.claude/settings.json`, `~/.codex/hooks.json`, or wherever your agent keeps hooks, and store the key with `python3 ~/limpet/limpet.py key set`.

## Rules

On first run limpet copies its default rules to `~/.limpet/rules.md`. Edit that file. Only lines starting with `- ` are rules. Any language works.

```markdown
- Don't say "done" without running the tests
- Don't ask "shall I start?" for work that was already requested. Only ask before irreversible actions
- Don't hand work to the human unless only a human can do it
- Fix problems you find before stopping. Don't stop at "CI is failing"
- When waiting, give a time estimate
```

The [default rules](rules.md) are the ten that the author's agents actually break.

## Find your rules

You don't have to guess which rules you need. limpet can read your own transcripts and tell you:

```sh
python3 ~/limpet/limpet.py suggest            # last 30 days; --days 90 --max 5000 to go wider
```

It finds every place an agent stopped and you replied, asks jev how you reacted (pushed it on, corrected it, asked, moved on) and what the agent got wrong, and writes `~/.limpet/suggest.md`:

```
## What went wrong (598 stops the human pushed back on)

### handoff: 259 (43%) — limpet can target this at stop time
- agent: I've prepared the command. Paste it and run it.
  human: can't you just write it to the file?
### overreach: 87 (15%) — limpet can target this at stop time
- agent: I also went ahead and invited the other projects…
  human: why are you widening this? nobody asked for that
### unverified: 60 (10%) — not visible at stop time
- agent: Pushed to PR #339. Three re-reviews, nothing required.
  human: ci failed

## Suggested rules
<!-- handoff: 43% of your bad stops -->
- Don't ask "shall I start?" for work that was already requested …
```

1,500 stops take about 100 seconds and 15 cents. It reads Claude Code and Codex transcripts. It is honest about the types no stop-time rule can see (a success claim that CI later disproves, a fix that is simply wrong) so you know what limpet will and won't catch.

## Thresholds

You don't have to wait for data either. `calibrate` scores your current `rules.md` against the same past stops and prints the thresholds:

```sh
python3 ~/limpet/limpet.py calibrate          # --fp 0.10 to accept 10% false positives instead of 5%
```

```
1500 scored in 198 s: 597 stops the human pushed back on, 903 fine.

 AUROC   thr  catches  blocks  rule
  0.62  0.42     18%      5%  Fix problems you find before stopping …
  0.60  0.49     10%      5%  Don't ask "shall I start?" …
  0.50  0.41      8%      6%  Don't say "done" without running the tests   (does not separate; left in shadow)

LIMPET_BLOCK="Fix problems=0.42,Don't ask=0.49,…"
```

AUROC is how well the rule separates stops you pushed back on from stops you were fine with (0.5 is a coin toss). `blocks` is the share of fine stops the threshold actually blocks, which can exceed the target when many stops tie. A rule below 0.55 is left out of `LIMPET_BLOCK`; it stays in `rules.md` and keeps being logged. Copy the last line into your settings.

Without `LIMPET_BLOCK` set, limpet runs in **shadow mode**: every stop is scored and logged to `~/.limpet/log.jsonl`, nothing is blocked.

`LIMPET_BLOCK` is one number for every rule, or a comma list of `substring of the rule=threshold`. Rules without a threshold never block.

## How well does it work

Honest numbers, measured on the author's own 40 days of Claude Code and Codex transcripts (2,645 stops the human replied to, after removing automation noise). A stop is "bad" when jev classifies the human's reply as pushing the agent on or correcting it, and the failure type is jev's too, so the labels are noisy and these numbers are a floor.

| Failure type (n) | Rule | AUROC | Caught at 5% false positives |
|---|---|---|---|
| handoff (646) | Don't hand work to the human | 0.62 | 8% |
| handoff | Don't ask "shall I start?" | 0.60 | 5% |
| handoff | try another way / fix before stopping / give an estimate | 0.57–0.58 | 6–8% |
| overreach (178) | Don't stop to offer things that weren't asked for | 0.60 | 12% |
| overreach | Don't widen the scope | 0.59 | 9% |
| misread (30) | Don't confuse a proposal with a request to act | 0.64 | 10% |
| taste (100) | Answer in the human's language | 0.51 | no signal |

0.5 is a coin toss. So: at a threshold that blocks 5% of fine stops, limpet catches 5–12% of the bad stops of that type, one to two and a half times what random blocking would. It is a cheap nudge, not a wall. On a smaller set labeled carefully by a large model the same rules scored 0.62–0.70, so the ceiling is probably around 0.65 with the information a stop has.

What it cannot see at all: a success report that CI later disproves, a fix that is simply wrong, the wrong repo, stale state, taste. Those were 33% of the author's bad stops. `suggest` tells you your own split.

## Compared to

Other Stop hooks that push the agent back fall into two camps.

- **Regex hooks** such as [checkpoint-guard](https://github.com/platcrest/checkpoint-guard), [llm-dark-patterns](https://github.com/waitdeadai/llm-dark-patterns) and [cc-enforcer](https://github.com/skymanbp/cc-enforcer). Free and instant, but they match English phrasings. Every new way of stopping early needs a new pattern, and rules in other languages are out.
- **Claude-as-judge hooks** such as Claude Code's built-in `type: "prompt"` hooks or [superpowers](https://github.com/obra/superpowers)' judge script. They understand the rule, but every stop costs a full model call in latency and money.

limpet sits in between. Rules are plain language in any language, judged by a model built for yes/no questions. Because it returns probabilities rather than verdicts, you set the threshold per rule from your own log instead of trusting a fixed prompt.

## Configuration

Where the key comes from, first match wins: the environment (or the Claude Code plugin config, or `KEY=VALUE` lines in `~/.limpet/env`), then the OS keychain written by `key set`, then `LIMPET_KEY_CMD`. Prefer the plugin config or the keychain; `~/.limpet/env` is plain text and only there for platforms without a keychain.

| Variable | Default | |
|---|---|---|
| `TYPESAFE_API_KEY` | | TypeSafe key. Calls `api.typesafe.ai` directly |
| `AI_GATEWAY_API_KEY` | | Vercel AI Gateway key. Either key is enough; TypeSafe wins if both are set |
| `key set` / `key rm` | | Store or remove the key in the OS keychain (`--provider vercel` for a gateway key) |
| `LIMPET_KEY_CMD` | | Shell command that prints the key, for password managers. `op read op://vault/item/password` |
| `LIMPET_PROVIDER` | `vercel` | `typesafe` or `vercel`. Only needed with `LIMPET_KEY_CMD` |
| `LIMPET_BLOCK` | unset | Thresholds, see above. Unset is shadow mode |
| `LIMPET_RULES` | `~/.limpet/rules.md` | Rules file |
| `LIMPET_LOG` | `~/.limpet/log.jsonl` | One JSON line per stop: probabilities, latency, tokens, what was blocked |
| `LIMPET_JEV_MODEL` | `jev-latest` / `typesafe-ai/jev` | Model id sent to the provider |

## Failure mode

No key, API error, or timeout: exit 0, silently. A hook must never block work because a service is down. Errors are recorded in the log.

## Privacy

Each stop sends the last three messages, one line per tool call of the current turn (tool name and its main argument, the last 40 calls), and the final message to the provider you chose. File contents and diffs are never sent.

`suggest` and `calibrate` read transcripts of **every** Claude Code project and Codex session on the machine (last 30 days by default) and send the same fields plus your own replies. Nothing else leaves the machine. The log and the suggestions stay in `~/.limpet`.

## Test

```sh
python3 test_limpet.py
```

Runs in under a second and calls no API. CI runs it on Linux and macOS, Python 3.9 and 3.13.

## License

MIT. Made by [no plan inc.](https://github.com/noplan-inc)
