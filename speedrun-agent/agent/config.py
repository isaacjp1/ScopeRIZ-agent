"""All tunables in one place. Every value can be overridden with an env var in the Dockerfile."""
import os
import sys


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


QUEUE_URL = os.environ.get("QUEUE_URL", "").rstrip("/")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
SITE_USERNAME = os.environ.get("SITE_USERNAME", "agent")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "agent")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
SHIFT_SECONDS = _f("SHIFT_SECONDS", 900)
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/work")

# Model choice: MODEL wins if set; otherwise the first of MODEL_PREFS the endpoint lists.
MODEL = os.environ.get("MODEL", "")
MODEL_PREFS = [m.strip() for m in os.environ.get(
    "MODEL_PREFS",
    "anthropic/claude-sonnet-5.5,google/gemini-3.8-flash,google/gemini-3.7-flash,google/gemini-3.6-flash,"
    "google/gemini-3.5-flash,google/gemini-3-flash-preview,google/gemini-2.5-flash,anthropic/claude-haiku-4.5",
).split(",") if m.strip()]
# The fast model the agent falls back to: once STRONG_BUDGET_SHARE of the budget is spent (Sonnet costs ~10x a flash
# turn, so the budget would not last the shift), or for good if the main model keeps failing.
FALLBACK_PREFS = [m.strip() for m in os.environ.get(
    "FALLBACK_PREFS",
    "google/gemini-3.8-flash,google/gemini-3.7-flash,google/gemini-3.6-flash,google/gemini-3.5-flash,"
    "google/gemini-2.5-flash,anthropic/claude-haiku-4.5",
).split(",") if m.strip()]
STRONG_BUDGET_SHARE = float(os.environ.get("STRONG_BUDGET_SHARE", "0.6"))
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "low")  # "", "low", "medium", "high". Thinking is most of a turn's time

# Time and risk policy
TASK_SECONDS = _f("TASK_SECONDS", 20)        # hard cap per task, then skip
MIN_TASK_SECONDS = _f("MIN_TASK_SECONDS", 4)  # never cap a task below this (unless the shift is ending)
TIME_STRETCH = _f("TIME_STRETCH", 2.5)        # a task may use this many times its fair share of the time left
MAX_TURNS = int(_f("MAX_TURNS", 10))         # LLM turns per task
MIN_CONFIDENCE = _f("MIN_CONFIDENCE", 0.7)   # answer only above this (wrong = -1)
SAFETY_SECONDS = _f("SAFETY_SECONDS", 3)     # stop this close to the end of the shift
PROBE = int(_f("PROBE", 10))                 # probe answers per stage code when startup fails; 0 = off (Phase 5 release build)

# Browser
PAGE_POOL = int(_f("PAGE_POOL", 6))          # parallel browser tabs
PAGE_CHARS = int(_f("PAGE_CHARS", 9000))     # max page text sent to the model per page
NAV_TIMEOUT = _f("NAV_TIMEOUT", 12)
CACHE_SECONDS = _f("CACHE_SECONDS", 60)      # a page read again after this long is reloaded (records change)

# Debug output: print what the model sees. "auto" = on only for the practice set (1200 s shift).
_dbg = os.environ.get("DEBUG_PAGES", "auto")
DEBUG = (SHIFT_SECONDS == 1200) if _dbg == "auto" else _dbg == "1"


# Known sets: shift length -> number of tasks (from the competition docs). Others: about 4 s per task.
KNOWN_SETS = {1200: 6, 900: 225, 1800: 458, 3600: 924}
# Model budget in dollars per set (from `arena sets`). The model loop stops at LOOP_BUDGET_SHARE of it so the cheap
# calls of the deterministic paths (rules, documents, phone screenshots) never hit the 402 wall.
BUDGETS = {1200: 1.0, 900: 8.0, 1800: 15.0, 3600: 25.0}
BUDGET = _f("BUDGET", BUDGETS.get(int(SHIFT_SECONDS), SHIFT_SECONDS / 3600 * 25))
LOOP_BUDGET_SHARE = _f("LOOP_BUDGET_SHARE", 0.9)
# Write a kind off (no model loop, SKIP_SECONDS) after this many skips in a row while time is short. Kinds are now
# rule-message kinds (REUSE-01) and an answer clears the skip run; the 100 of 191 Set 1 skips at 3 came from the old
# keyword-signature grouping, which pooled unrelated tasks.
HOPELESS_AFTER = int(_f("HOPELESS_AFTER", 3))
TIGHT_FAIR_SECONDS = _f("TIGHT_FAIR_SECONDS", 6)
# A written-off kind still gets one longer model-loop try every EXPLORE_EVERY of its tasks, when the time left covers
# the try plus SKIP_SECONDS for each task after it (Set 3 closed every task with time to spare: kinds written off
# after 3 short tries were never tried again). One success makes the kind normal again, with its route as a hint.
EXPLORE_EVERY = int(_f("EXPLORE_EVERY", 6))
EXPLORE_SECONDS = _f("EXPLORE_SECONDS", 30)
SKIP_SECONDS = _f("SKIP_SECONDS", 2)
# REUSE-02: step() calls in one stored-program run. Each call may ask for pages (search, chart, tab, answer).
PROGRAM_STEPS = int(_f("PROGRAM_STEPS", 4))
# REUSE-02: model calls that write a stored program, per kind label. Each is about one strong-model turn with a long
# prompt (rule, transcript, page samples); 0 turns learning off (the Phase 3 behaviour).
LEARN_TRIES = int(_f("LEARN_TRIES", 2))


def require_urls():
    """Rule 1: the judge passes all three addresses; never fall back to a public host."""
    missing = [k for k in ("QUEUE_URL", "SITE_URL", "LLM_BASE_URL") if not globals()[k]]
    if missing:
        sys.exit("[agent] missing environment variable(s): " + ", ".join(missing))


def expected_tasks():
    return KNOWN_SETS.get(int(SHIFT_SECONDS), max(1, int(SHIFT_SECONDS / 4)))


def task_cap(seconds_left, tasks_left, written_off=0.0):
    """Seconds this task may take: a multiple of its fair share of the time left, between MIN and TASK_SECONDS.
    Not reaching a task scores the same as skipping it, so slow tasks must not eat the time of many quick ones.
    The written_off share hands the time written-off kinds free to the tasks that are still tried: that share of the
    remaining tasks is charged SKIP_SECONDS each, and the rest of the time is shared by the others."""
    gone = written_off * tasks_left
    fair = (seconds_left - gone * SKIP_SECONDS) / max(1, tasks_left - gone)
    return max(MIN_TASK_SECONDS, min(TASK_SECONDS, TIME_STRETCH * fair))
