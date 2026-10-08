# Speedrun agent (Zeren)

A Python agent: a headless browser that stays signed in and opens pages by URL, plus a short LLM tool loop
for each task. Messages are acknowledged at once and kept. The agent skips when unsure (wrong = -1), and it remembers
which pages answered earlier tasks of the same kind, so later ones go faster.

## One-time setup (Mac / Windows / Linux)
1. Install **Docker Desktop** and open it (the whale icon should say "running").
2. On speedrun.scopehealth.com, open **Set up**, copy the install command and run it in Terminal.
3. Then:
   ```
   cd speedrun-kit
   export PATH="$PWD:$PATH"
   ```
4. Unzip `speedrun-agent.zip` inside `speedrun-kit`, so you have `speedrun-kit/speedrun-agent/`.

## Every iteration
```
docker build --platform linux/amd64 -t zeren-agent speedrun-agent
docker run --rm -v "$PWD/speedrun-agent:/src" -w /src zeren-agent python3 test/test_logic.py   # 2 s self-check, must print "all logic checks passed"
arena test --set practice --agent zeren-agent        # ~up to 20 min, prints the log + record
arena runs                                            # list runs
arena runs <run id>                                   # every item, our answer, expected answer
```
Keep the output of `arena runs <run id>` and the `[agent]` log lines for review.

## Submitting (2 per day; the LATEST finished submission is what counts, even if lower)
```
arena push zeren-agent            # prints an image id
arena submit --image <id> --wait
arena leaderboard
```

## Knobs (edit the ENV lines in the Dockerfile)
| Variable | Default | Meaning |
|---|---|---|
| MODEL | auto | Pin an OpenRouter model id, e.g. google/gemini-2.5-flash |
| TASK_SECONDS | 20 | Never spend longer than this on one task |
| MIN_TASK_SECONDS | 4 | Floor of the per-task time cap |
| TIME_STRETCH | 2.5 | A task may use this many times its fair share (time left / tasks left) |
| MAX_TURNS | 10 | Max model calls per task |
| MIN_CONFIDENCE | 0.7 | Only answer above this self-rated confidence |
| REASONING_EFFORT | low | low / medium / high for reasoning models (thinking is most of a model turn's time) |
| AGENT_NOTES | (empty) | Extra tips added to every prompt |
| BUDGET | per set | Model budget in $ (known sets are detected from SHIFT_SECONDS) |
| LOOP_BUDGET_SHARE | 0.9 | The model loop stops at this share of BUDGET |
| HOPELESS_AFTER | 3 | Write a rule-message kind off (2 s, no model loop) when time is short and this many of its tasks in a row were skipped |
| TIGHT_FAIR_SECONDS | 6 | "Time is short" = fewer seconds than this left per remaining task |
| LEARN_TRIES | 2 | Model calls that write a stored program per kind (REUSE-02); 0 = no stored programs |

## Optional: try it on your machine against the practice clinic
```
docker run --rm -e LLM_API_KEY=<your dev key> \
  -v "$PWD/speedrun-agent/practice_items.json:/app/items.json" \
  zeren-agent python3 -m agent.local /app/items.json
```
