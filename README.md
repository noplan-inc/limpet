# limpet

A Claude Code Stop hook that stops the agent from stopping too early.

You write rules in plain language in `rules.md`. Every time Claude Code is about to stop, limpet sends the last few turns to [jev](https://typesafe.ai) (TypeSafe AI's evaluation model, via [Vercel AI Gateway](https://vercel.com/ai-gateway)) and gets, for each rule, the probability that it was just violated. If a rule is over its threshold, the agent is told to keep working instead of stopping.

One Python file, standard library only. No regexes to maintain, no local model, no training.

## Why

Coding agents stop early. "I'll wait for CI." "Shall I run it?" "Please ask X about this." You type "do it". Then again. Limpet catches those stops before you see them and pushes the agent back to work, so it clings to the task like a limpet to a rock.

In practice it also misfires sometimes. That costs the agent one line ("this rule doesn't apply because…") and then it stops normally, so false positives are cheap. Missing a real early stop costs you a round trip, so tune for recall.

## Install

1. Get a Vercel AI Gateway key. jev costs $0.042 per million input tokens; a stop is 1,000 to 2,000 tokens, so a few cents per day of heavy use.
2. Clone this repo:

   ```sh
   git clone https://github.com/noplan-inc/limpet ~/limpet
   ```

3. Add the hook to `~/.claude/settings.json` (or a project's `.claude/settings.json`):

   ```json
   {
     "env": {
       "AI_GATEWAY_API_KEY": "vck_...",
       "LIMPET_BLOCK": "0.8"
     },
     "hooks": {
       "Stop": [{ "hooks": [{ "type": "command", "command": "python3 ~/limpet/jev_stop.py", "timeout": 30 }] }]
     }
   }
   ```

   If you would rather not put the key in a file, set `LIMPET_KEY_CMD` to a shell command that prints it, for example `op read op://vault/vercel-ai-gateway/password`.

4. Edit `rules.md`. Only lines starting with `- ` are rules. Any language works.

## Thresholds

`LIMPET_BLOCK` is either one number for every rule, or a comma list of `substring of the rule=threshold`:

```
LIMPET_BLOCK="shall I start=0.30,hand work=0.60,before stopping=0.35,time estimate=0.74"
```

Rules without a threshold never block. Leave `LIMPET_BLOCK` unset to run in shadow mode: every stop is scored and logged to `~/.limpet/log.jsonl`, nothing is blocked. After a day or two:

```sh
python3 ~/limpet/jev_stop.py --stats
```

prints per-rule percentiles. A threshold around p95 blocks the worst 5% of stops for that rule, which is where the author started.

## What the agent sees

On a hit, the hook exits 2 with this on stderr, which Claude Code feeds back to the agent:

> limpet: this response may violate: "Fix problems you find before stopping…" (83%). If it does, follow the rule and keep working. If it does not, say why in one line, then stop.

The second stop of the same chain has `stop_hook_active` set and is always allowed through, so the agent is pushed back at most once per stop.

## Failure mode

No key, API error, or timeout: exit 0, silently. The hook must never block work because a service is down. Errors are recorded in the log.

## Environment

| Variable | Default | |
|---|---|---|
| `AI_GATEWAY_API_KEY` | | Vercel AI Gateway key |
| `LIMPET_KEY_CMD` | | Shell command that prints the key, used when the variable above is unset |
| `LIMPET_BLOCK` | unset | Thresholds, see above |
| `LIMPET_RULES` | `rules.md` next to the script | Rules file |
| `LIMPET_LOG` | `~/.limpet/log.jsonl` | Log file, one JSON line per stop |
| `LIMPET_JEV_MODEL` | `typesafe-ai/jev` | Model id sent to the gateway |

## Test

```sh
python3 test_jev_stop.py
```

Does not call the API.

## License

MIT
