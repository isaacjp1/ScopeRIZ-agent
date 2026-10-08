"""Mock shifts end to end: starts test/mock_wrenfield.py with fault switches, runs the agent against it, and checks
the agent output and the mock queue's close log.

    python3 test/test_shift.py [scenario ...]        # no names: every scenario
    # browser scenarios need Playwright, so run them in the image:
    docker run --rm -v "$PWD/speedrun-agent:/src" -w /src zeren-agent python3 test/test_shift.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# name -> mock: MOCK_* env for the mock; agent: env overrides for the agent; browser: needs Playwright;
# probes: None, or (count, value) expected among the answers that start "PROBE-".
# Optional: log / not_log: strings that must / must not appear in the agent output; correct: minimum correct non-probe
# answers; must_answer: answers that must be among the correct ones; max_wrong: most non-probe answers allowed wrong.
SCENARIOS = {
    # Queue down from second 15 to 30, model listing 20 s late, sign-in 15 s late. MODEL is unset so listing runs, and
    # MODEL_PREFS=fake makes it pick the mock's model (an anthropic/ id would break the scripted fake model).
    "robust": {
        "mock": {"MOCK_SHIFT": "60", "MOCK_QUEUE_ERRORS": "15-30", "MOCK_SLOW_LOGIN": "15", "MOCK_SLOW_MODELS": "20"},
        "agent": {"MODEL": "", "MODEL_PREFS": "fake"},
        "browser": False,
        "probes": None,
    },
    # Nothing listens on the site port. Without Playwright the browser start fails; with it every page load fails;
    # both read as signed_in False, location_ok False and a miss for each of the mock's 5 dates of birth.
    "dead-site": {
        "mock": {"MOCK_SHIFT": "90"},
        "agent": {"SITE_URL": "http://127.0.0.1:8819"},
        "browser": False,
        "probes": (6, "PROBE-SIGN-IN"),
        "log": ["[degraded] sign-in"],
    },
    # Browser scenarios (Plan 03 runs them in the image). A MOCK_SHIFT may be raised if a run ends before its probes
    # are all sent (5 misses plus the probe count in skips are needed); a probe count is never changed.
    "clean": {
        "mock": {"MOCK_SHIFT": "60"},
        "agent": {},
        "browser": True,
        "probes": (0, None),
        "not_log": ["[degraded]"],
    },
    # Sign-in POST answers after 15 s, longer than the 12 s navigation timeout (Phase 1 deferred item).
    "slow-sign-in": {
        "mock": {"MOCK_SHIFT": "90", "MOCK_SLOW_LOGIN": "15"},
        "agent": {},
        "browser": True,
        "probes": (0, None),
        "log": ["sign-in=True", "[probe] startup stage=ok"],
        "not_log": ["[degraded]"],
        "correct": 1,
    },
    # The session stays at Northgate: Judith Jackson and Gregory Porter hit, Carol Jones, Peter Henry and Laura Torres
    # miss, so the verdict is "location" once the fifth date of birth is searched (2 hits, 3 misses).
    "location": {
        "mock": {"MOCK_SHIFT": "90", "MOCK_BREAK_LOC": "1"},
        "agent": {},
        "browser": True,
        "probes": (2, "PROBE-LOCATION"),
    },
    "search": {
        "mock": {"MOCK_SHIFT": "90", "MOCK_BREAK_SEARCH": "1"},
        "agent": {},
        "browser": True,
        "probes": (4, "PROBE-SEARCH"),
    },
    "sign-in": {
        "mock": {"MOCK_SHIFT": "120", "MOCK_BREAK_LOGIN": "1"},
        "agent": {},
        "browser": True,
        "probes": (6, "PROBE-SIGN-IN"),
        "log": ["[degraded] sign-in"],
    },
}

TASK_LINE = re.compile(r"^\[agent\] \S+ (?:SKIP|ANSWER) .* in (\d+(?:\.\d+)?)s \|", re.M)


def run(name, sc):
    if sc["browser"] and importlib.util.find_spec("playwright") is None:
        print(f"[shift] {name} skipped (needs Playwright; run in the Docker image)", flush=True)
        return
    shift = float(sc["mock"]["MOCK_SHIFT"])
    mock = subprocess.Popen([sys.executable, "test/mock_wrenfield.py"], cwd=ROOT, env={**os.environ, **sc["mock"]},
                            stdout=subprocess.PIPE, text=True)
    try:
        t_up = time.time() + 10
        while time.time() < t_up:
            line = mock.stdout.readline()
            if "mock wrenfield up" in line:
                break
        else:
            raise AssertionError(f"{name}: mock did not start")
        env = {**os.environ, "QUEUE_URL": "http://127.0.0.1:8812", "SITE_URL": "http://127.0.0.1:8811",
               "LLM_BASE_URL": "http://127.0.0.1:8813/v1", "LLM_API_KEY": "x", "MODEL": "fake",
               "SHIFT_SECONDS": str(int(shift)), "PROBE": "2", "WORK_DIR": tempfile.mkdtemp(), **sc["agent"]}
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "agent.main"], cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=shift + 120)
        wall = time.time() - t0
        closes = json.load(urllib.request.urlopen("http://127.0.0.1:8812/log", timeout=10))
    finally:
        mock.terminate()
        mock.wait(10)
    out = r.stdout + r.stderr
    tail = "\n".join(out.splitlines()[-40:])
    assert r.returncode == 0, tail
    assert wall >= shift - 5, f"agent stopped after {wall:.0f}s of a {shift:.0f}s shift\n{tail}"
    assert "[agent] done:" in out, tail
    assert closes and closes[-1]["t"] >= shift - 20, f"last close at {closes[-1]['t'] if closes else None}\n{tail}"
    probes = [c["answer"] for c in closes if str(c.get("answer", "")).startswith("PROBE-")]
    if sc["probes"]:
        count, value = sc["probes"]
        assert len(probes) == count, f"{len(probes)} probe answers, expected {count}\n{tail}"
        assert count == 0 or set(probes) == {value}, f"probe answers {probes}\n{tail}"
    if name == "robust":
        assert any(c["t"] > 30 for c in closes), f"no close after the outage\n{tail}"
        assert "; retrying" in out, tail
        first = TASK_LINE.search(out)
        assert first and float(first.group(1)) <= 21, f"first task line: {first and first.group(0)}\n{tail}"
    if name == "clean":
        assert any(c.get("answer") and not str(c["answer"]).startswith("PROBE-") for c in closes), f"no answer\n{tail}"
        assert "[site] blocked a request to blocked.example" in out, tail
        assert "[probe] startup stage=ok" in out, tail
        assert any("sign-in=True" in ln and "nav=" in ln for ln in out.splitlines() if "[site] ready in" in ln), tail
        assert "[kinds] " in out, tail
    if name == "dead-site":
        assert "[kinds] " in out, tail
        assert "[probe] startup stage=sign-in" in out, tail
        # every probe close keeps its per-task line
        lines = out.count("SKIP 'PROBE-SIGN-IN' in ")
        assert lines == len(probes), f"{lines} probe task lines for {len(probes)} probe answers\n{tail}"
    for want in sc.get("log", []):
        assert want in out, f"missing {want!r} in the output\n{tail}"
    for bad in sc.get("not_log", []):
        assert bad not in out, f"unexpected {bad!r} in the output\n{tail}"
    answers = [c for c in closes if "answer" in c and not str(c["answer"]).startswith("PROBE-")]
    right = [str(c["answer"]) for c in answers if str(c["answer"]) == str(c.get("expected"))]
    wrong = [(c["answer"], c.get("expected")) for c in answers if str(c["answer"]) != str(c.get("expected"))]
    assert len(right) >= sc.get("correct", 0), f"{len(right)} correct answers, expected at least {sc['correct']}\n{tail}"
    for want in sc.get("must_answer", []):
        assert want in right, f"no correct answer {want!r} (correct: {right})\n{tail}"
    if "max_wrong" in sc:
        assert len(wrong) <= sc["max_wrong"], f"{len(wrong)} wrong answers {wrong}\n{tail}"
    print(f"[shift] {name} ok closes={len(closes)} probes={len(probes)} last_t={closes[-1]['t']} "
          f"correct={len(right)} wrong={len(wrong)}", flush=True)
    # echoed for Plan 03's record (routing cost, RESEARCH Pitfall 1)
    lines = out.splitlines()
    for tag in ("[site] ready in", "[probe] startup", "[kinds] ", "[degraded]"):
        hit = [ln for ln in lines if ln.startswith(tag)]
        if hit:
            print(f"   {hit[-1][:200]}", flush=True)
    secs = [float(s) for s in re.findall(r"^\[agent\] \S+ ANSWER .* in (\d+(?:\.\d+)?)s \|", out, re.M)]
    print(f"   answered={len(secs)} mean={sum(secs) / len(secs) if secs else 0:.1f}s", flush=True)


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for n in names:
        run(n, SCENARIOS[n])
    print("all shift scenarios passed")
