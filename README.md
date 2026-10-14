# ScopeRIZ Speedrun Agent

Our entry for the **Scope Health Speedrun** competition (Oct 5–9, 2026). It is a Docker-packaged Python agent that works one shift of a clinic back-office queue inside **Wrenfield**, a web records system with patients, charts, a schedule, billing and documents.

Each queue item is plain text and is one of two kinds:

- **Message**: a rule, workflow, fact, rule change or noise. The agent acknowledges it and keeps it, because later tasks may depend on it.
- **Task**: a question with one answer (a phone number, a count, a date, a claim's next step, and so on). The agent answers or skips.

Scoring is **+1 correct, −1 wrong, 0 skipped or not reached**. The agent answers only when it is confident, and it has to work fast: each set has a fixed shift length and model budget.

| Set | Length | Tasks | Model budget |
|---|---|---|---|
| practice | 20 min | 6 | $1 |
| public (Set 1) | 15 min | 224 | $8 |
| private-a (Set 2) | 15 min | 226 | $8 |
| private-b (Set 3) | 30 min | 458 | $15 |
| final (hidden) | 60 min | 924 | $25 |

The final ranking is the latest submission's score plus one run on the hidden final set. Competition docs: [`speedrun.md`](speedrun.md) (getting started), [`rules.md`](rules.md), [`reference_agent.md`](reference_agent.md).

## Repository layout

```
speedrun-agent/        The agent (the thing we build and submit)
  agent/               Python package, run as `python3 -m agent.main`
  test/                Offline self-checks and mock Wrenfield servers
  eval/                Run-log parser and fixtures
  Dockerfile           Image definition; ENV lines are the tuning policy
  README.md            Build / test / submit cheat sheet and knobs
  ARCHITECTURE.md      Design notes
  COMPETITION_NOTES.md Interface, answer comparison and scoring details
  EXPERIMENTS.md       Every scored build and its per-set results
speedrun-kit/          Vendor kit: `arena` CLI, docs, TypeScript reference agent
```

Old submission snapshots (`v*-agent/`, `speedrun-agent-v*.zip`, `speedrun-kit/v59/`) stay local and are git-ignored.

## How the agent works

The protocol holds one item at a time, so there is no task concurrency. Speed comes from deterministic code paths, a warm browser and page caching.

1. **Start-up** (`main.py`, `site.py`): sign in to Wrenfield with headless Chromium (Playwright), switch to the "all locations" view, and warm up a pool of tabs. Tasks wait on a `site.ready` event. Start-up is time-bounded so a slow sign-in can't eat the shift.
2. **Messages** (`rules.py`): acknowledged at once and stored word for word, in order. Rules the code needs, such as the new-vs-established window, are compiled to parameters by the model in the background. A parameter is accepted only if its value appears in the message text (`check_ne`).
3. **Tasks** (`solver.py`): each task runs under a time cap (`config.task_cap`: a stretch of the fair share of the time left, clamped to a min and max).
   - **Fast paths first** (`fast.py`, `solvers.py`, `clinic.py`): a regex picks the task kind, records confirm the patient, provider or claim, and code computes the answer. Kinds include new/established patient, phone numbers (page digits are scrambled, so they are checked against a screenshot read), document questions (PDF text via `pypdf`), provider appointment counts, open slots, filing deadlines and claim next steps. A path returns `None` when it is unsure.
   - **Model tool loop** as fallback: a short OpenAI-compatible chat loop with browsing tools (`open page`, `find_patient`, `read_document`, a sandboxed Python scratch tool, ...). Routes that worked are kept in `memory.py` as hints for later tasks of the same kind.
   - **Gate**: answers below `MIN_CONFIDENCE` become skips. Kinds that keep failing are written off when time is short.
4. **Model access** (`llm.py`): model preference lists in `config.py`, budget accounting (`BudgetExhausted` turns into skips), a slot limiter for the proxy's 429 concurrency limit, and bounded retries.

Results are triples `(kind, value, info)`: `kind` is `"answer"` or `"skip"`, and `info` carries `why`, `conf` and `route`.

### Competition constraints built into the code

- **Rule 1 (network)**: the agent may contact only `QUEUE_URL`, `SITE_URL` and `LLM_BASE_URL`. The browser aborts requests to any other host. The Python scratch tool runs without secrets or sockets. Start-up fails fast if a URL is missing.
- **Never quit early**: queue errors mid-shift are retried. The loop runs until the queue closes or time runs out.
- **Start-up probe**: scored Sets 2 and 3 show only point counts, no logs. An optional probe sends a fixed number of deliberate answers when a start-up stage fails, so the wrong-answer count shows which stage broke (see `EXPERIMENTS.md`).

## Build, test, submit

Full details are in [`speedrun-agent/README.md`](speedrun-agent/README.md). In short:

```bash
# self-check (no browser, no network), must print "all logic checks passed"
cd speedrun-agent && python3 test/test_logic.py

# build for the judge (linux/amd64 only)
docker build --platform linux/amd64 -t zeren-agent speedrun-agent

# practice run and inspection (vendor CLI, see speedrun-kit/)
arena test --set practice --agent zeren-agent
arena runs <run id>

# submit (only the latest finished submission counts)
arena push zeren-agent
arena submit --image <id> --wait
arena leaderboard
```

`test/test_shift.py` drives whole mock shifts against `test/mock_wrenfield.py` and `test/mock_set1.py`. Run it inside the Docker image when you need Playwright.

## Configuration

Set by the judge: `QUEUE_URL`, `SITE_URL`, `SITE_USERNAME`, `SITE_PASSWORD`, `LLM_BASE_URL`, `LLM_API_KEY`, `SHIFT_SECONDS`, `WORK_DIR`.

Tunables are `ENV` lines in `speedrun-agent/Dockerfile`, which override the defaults in `agent/config.py`. The main ones are `TASK_SECONDS`, `MIN_TASK_SECONDS`, `TIME_STRETCH`, `MAX_TURNS`, `MIN_CONFIDENCE`, `PAGE_POOL`, `MODEL` / `MODEL_PREFS`, `REASONING_EFFORT`, `BUDGET` and `LOOP_BUDGET_SHARE`. The table in `speedrun-agent/README.md` explains each. Secrets come only from the environment and are never logged.

## Tech stack

- Python 3.12 (`python:3.12-slim`), asyncio, stdlib `urllib` for HTTP
- Playwright 1.56.0 (headless Chromium), pypdf 5.1.0, tzdata 2025.2, pinned in the Dockerfile
- OpenRouter-compatible model endpoint (Claude Sonnet / Gemini Flash family by default)
- No test framework: plain `assert` scripts

## History and results

Work ran in five phases:

1. Safe, bounded shift with a start-up probe
2. Works at unseen clinics (sign-in, location, search and routes across Wrenfield versions)
3. Time follows answers (doomed kinds skipped fast, time moved to kinds that answer)
4. Learn once, reuse per kind (the first solved task of a kind leaves a checked program)
5. Release the best verified build

The milestone started from `baseline-128` (128 points across the three scored sets). Later builds traded wins between sets: for example, v51 scored 67 on Set 3 and v54 scored 83 on Set 2. v55 and v61 combine their strengths. `speedrun-agent/EXPERIMENTS.md` holds the per-build hypotheses, scores and post-mortems.
