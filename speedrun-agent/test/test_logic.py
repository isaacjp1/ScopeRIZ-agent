"""Self-checks for the deterministic logic (no browser, no network): python3 test/test_logic.py"""
import asyncio
import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
from agent import clinic, config  # noqa: E402
from agent.fast import DOC_TASK, NE_TASK, PHONE_OTHER, PHONE_TASK, WHO_REFERRED, dates_in, kind_of  # noqa: E402
from agent.main import kind_label  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.rules import Rules, check_ne  # noqa: E402
from agent.site import from_site, learn_routes, same_origin, to_site  # noqa: E402
from agent.solver import (PROBE_CODES, Solver, checked_answer, plausible, run_python, same_answer,  # noqa: E402
                          startup_probe)

MSG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "practice_items.json")))[0]["text"]
GOOD = {"years": 3, "counting_statuses": ["Completed"], "excluded_services": ["Lab draw", "X-ray"],
        "same_specialty_counts": True, "all_locations": True, "window": "standard", "new_answer": "NEW",
        "established_example": "ESTABLISHED:2025-03-11", "other_conditions": []}


def main():
    # rule compile check: accepts the practice rule, rejects anything not in the text or unusual
    r = check_ne(GOOD, MSG)
    assert r == {"years": 3, "statuses": ["Completed"], "excluded": ["Lab draw", "X-ray"], "new": "NEW", "prefix": "ESTABLISHED:"}, r
    assert check_ne({**GOOD, "excluded_services": ["Lab draw", "Massage"]}, MSG) is None
    assert check_ne({**GOOD, "other_conditions": ["telehealth never counts"]}, MSG) is None
    assert check_ne({**GOOD, "established_example": "EST:2025-03-11"}, MSG) is None
    assert check_ne({**GOOD, "years": "x"}, MSG) is None
    assert check_ne(None, MSG) is None

    # 3-year window, Feb 29
    assert clinic.window(dt.date(2026, 11, 5), 3) == (dt.date(2023, 11, 6), dt.date(2026, 11, 4))
    assert clinic.window(dt.date(2028, 2, 29), 3) == (dt.date(2025, 3, 1), dt.date(2028, 2, 28))

    # task routing
    t1 = "Is Judith Jackson (born 2004-11-12) new or established for Kenneth Sullivan, NP on 2028-07-20?"
    t2 = "On 2026-12-25 there is a visit with Douglas Hughes, MD for Henry, Peter, DOB 1975-06-23, and I need new vs. established for it."
    t3 = "What phone number do we have on file for patient Carol Jones (born 1982-09-07)?"
    t4 = "Referral letter received on 2025-09-16 for patient Gregory Porter (born 1976-04-08): what reason does it give for the referral?"
    assert NE_TASK.search(t1) and NE_TASK.search(t2) and not NE_TASK.search(t3)
    assert PHONE_TASK.search(t3) and not PHONE_OTHER.search(t3)
    assert PHONE_OTHER.search("What is the phone number of the practice that referred Carol Jones (born 1982-09-07)?")
    assert DOC_TASK.search(t4) and not DOC_TASK.search(t3)
    # DIAG-02: kind_of is the label of the deterministic path that takes a task ("" for none), in Fast.solve's order
    assert kind_of(t1) == "new_established" and kind_of(t3) == "phone" and kind_of(t4) == "document"
    assert kind_of("How many open slots are there for Logan Brown, MD at Northgate on 2026-10-14?") == "open_slots"
    assert kind_of("What is the next step on claim C-1?") == "claim_next_step"
    assert kind_of("How many patients with the last name Jones can you see?") == ""
    assert WHO_REFERRED.search("Referral letter received on 2024-12-01 for patient Ana Corbett: which practice made the referral?")
    assert not WHO_REFERRED.search(t4)
    m = re.search(r"(?:^| \| )referral letter from (.+?)(?: \||$)","Dec 1, 2024 | Referral letter | Referral letter from Granite Fork Internal Medicine | View", re.I)
    assert m and m.group(1) == "Granite Fork Internal Medicine", m
    assert dates_in(t2) == [dt.date(2026, 12, 25), dt.date(1975, 6, 23)]
    assert dates_in("seen Mar 4, 2026 and 03/05/2026") == [dt.date(2026, 3, 4), dt.date(2026, 3, 5)]

    # answer shape checks
    assert plausible("How many patients named Jones?", "12") and not plausible("How many patients named Jones?", "twelve")
    assert plausible(t3, "(785) 310-6041") and not plausible(t3, "310-6041")
    assert not plausible("Who referred her?", "None") and not plausible("x", "")
    assert plausible("Which providers?", ["A", "B"])

    # phone: the learned digit map is trusted only after 3 agreeing screenshots, and re-checked every 8th read
    class FakeSite:
        digit_map, digit_map_ok, digit_map_hits, phone_reads = {}, True, 0, 0
        shots = 0
        scramble = str.maketrans("0123456789", "9876543210")
        true_phones = {"/patients/a": "(785) 310-6041", "/patients/b": "(620) 555-0198", "/patients/c": "(719) 244-3871"}

        def _normalize(self, p):
            return p

        async def read(self, path, all_pages=True):
            return {"text": "Contact\nPhone\n" + self.true_phones[path].translate(self.scramble) + "\n"}

        async def screenshot(self, path):
            self.shots += 1
            self.last = path
            return "data:image/jpeg;base64,xx"

    class FakeLLM:
        async def chat(self, messages, **kw):
            return {"content": json.dumps({"phone": site.true_phones[site.last]})}, {}, 0

    site, llm = FakeSite(), FakeLLM()

    async def run():
        out = []
        for p in ["/patients/a", "/patients/b", "/patients/c", "/patients/a", "/patients/b", "/patients/c", "/patients/a"]:
            res = await clinic.patient_contact(site, llm, p)
            out.append((res["phone"], res["safe_to_answer"]))
        return out
    res = asyncio.run(run())
    assert all(ok and phone == site.true_phones[p] for (phone, ok), p in
               zip(res, ["/patients/a", "/patients/b", "/patients/c", "/patients/a", "/patients/b", "/patients/c", "/patients/a"])), res
    assert site.shots < 7, site.shots     # later reads decoded without a screenshot

    assert config.task_cap(900, 225) == 10.0 and config.task_cap(30, 225) == config.MIN_TASK_SECONDS

    # TIME-01/02: the per-kind time policy, with the tunables pinned so the environment cannot move them
    config.HOPELESS_AFTER, config.TIGHT_FAIR_SECONDS, config.EXPLORE_EVERY, config.EXPLORE_SECONDS = 3, 6, 6, 30
    config.SKIP_SECONDS, config.MIN_TASK_SECONDS, config.TASK_SECONDS, config.TIME_STRETCH = 2, 4, 20, 2.5
    assert config.task_cap(900, 225) == 10.0 == config.task_cap(900, 225, 0.0)
    assert config.task_cap(900, 225, 0.5) == 15.0 and config.task_cap(30, 225, 0.5) == config.MIN_TASK_SECONDS
    mem = Memory()
    assert mem.allot("k", 900, 225) == ("new", 10.0)
    mem.outcome("k", False)
    mem.outcome("k", False)
    assert mem.allot("k", 900, 225)[0] == "new"          # 2 skips in a row: one short of HOPELESS_AFTER
    mem.outcome("k", False)
    assert mem.allot("k", 1350, 225)[0] == "new"         # fair share exactly 6.0 is not time-short
    assert mem.allot("k", 900, 225) == ("written-off", 2)
    assert [mem.allot("k", 900, 225)[0] for _ in range(4)] == ["written-off"] * 4
    assert mem.allot("k", 900, 225) == ("retry", 30)      # the 6th written-off task
    assert [mem.allot("k", 900, 225)[0] for _ in range(5)] == ["written-off"] * 5
    assert mem.allot("k", 460, 225)[0] == "written-off"  # 12th: 460 - 30 = 430 is not > 225 * 2
    for _ in range(3):
        mem.outcome("j", False)
    assert mem.allot("j", 900, 225)[0] == "written-off"  # labels are separate: j's skips are not k's
    mem.outcome("k", True)                               # an answer clears k's skip run
    mode, cap = mem.allot("k", 900, 225)
    assert mode == "known" and cap >= 10.0, (mode, cap)
    assert mem.allot("j", 900, 225)[0] == "written-off"
    mem = Memory()
    mem.outcome("a", True)
    assert mem.allot("a", 900, 225) == ("known", 10.0)   # no task seen yet: written-off share 0
    mem.tasks, mem.written_off = 4, 2
    assert mem.allot("a", 900, 225) == ("known", 15.0)   # half written off: their time goes to the rest
    mem.outcome("b", True)
    for _ in range(3):
        mem.outcome("b", False)
    assert mem.allot("b", 900, 225)[0] == "written-off"  # the write-off check comes before "known"

    # REUSE-01: a task's kind is the long rule message it best matches (the for_task IDF ranking)
    here = os.path.dirname(__file__)
    filing = open(os.path.join(here, "set1_filing_rule.md"), encoding="utf-8").read()
    claim = open(os.path.join(here, "set1_claim_rule.md"), encoding="utf-8").read()
    vending = "The vending machine by the back entrance takes cards again, but the coin slot is still jammed."
    claims = ["Next step on claim: Coleman, Linda, DOB 1958-09-16, service date 2026-05-11",
              "Could you look at the claim for Linda Coleman, born 1958-09-16, with the date of service 2026-05-12, "
              "and tell me the next step?",
              "I'm going through the claim for Harold Wells with the date of service 2026-07-24. Where does it go from here?"]
    filings = ["Anderson, Alexander, DOB 1971-07-30, date of service 2026-08-31. We haven't billed this one yet. "
               "What's the deadline to file?",
               "Alexander Anderson, born 1971-07-30, has a visit from 2026-06-22 on the not-yet-filed list. Could you "
               "work out the filing deadline before I send it?",
               "Filing deadline for Kenneth Howell, DOS 2025-12-18?"]
    nes = [t1, "Billing question: Guadalupe Driscoll, visit with Sharon Morales, MD on 2025-11-05. Is that a new or an "
               "established patient visit?"]
    jones = "How many patients with the last name Jones can you see?"
    rules = Rules(None)   # items set directly: Rules.add starts a compile task, which needs a running event loop
    rules.items = [MSG, vending, filing, claim]
    assert [rules.kind_for(t) for t in claims] == [4, 4, 4], [rules.kind_for(t) for t in claims]
    assert [rules.kind_for(t) for t in filings] == [3, 3, 3], [rules.kind_for(t) for t in filings]
    assert [rules.kind_for(t) for t in nes] == [1, 1], [rules.kind_for(t) for t in nes]
    assert rules.kind_for("zzz qqq") == 0 and rules.kind_for(claims[0].upper()) == 4
    assert kind_label(rules, claims[0]) == ("m4/claim_next_step", 4)
    assert kind_label(rules, "zzz qqq") == ("unmatched", 0)
    assert kind_label(Rules(None), t3) == ("phone", 0)
    rules.items = [claim, claim]
    assert rules.kind_for(claims[0]) == 2                # a tie goes to the newer message
    rules.items = [MSG]
    assert rules.kind_for(jones) == 1                    # one long message: any shared word assigns the task
    rules.items = [vending]
    assert rules.kind_for(claims[0]) == 0                # short messages never become kinds
    rules.items = []
    assert rules.kind_for(claims[0]) == 0

    # Rule 1, python tool: the child sees no credentials (nor any other parent variable) and no network
    os.environ.update({"LLM_API_KEY": "sk-test", "SITE_PASSWORD": "pw-test", "ZZ_DECOY": "d"})
    os.makedirs(config.WORK_DIR, exist_ok=True)   # run_python writes pages.json there
    out = run_python("import os; print(*(os.environ.get(k) for k in ('LLM_API_KEY', 'SITE_PASSWORD', 'ZZ_DECOY')))", {})
    assert out.strip() == "None None None", out
    out = run_python("import urllib.request; urllib.request.urlopen('http://127.0.0.1:9')", {})
    assert "network is disabled in the python tool" in out, out
    out = run_python("import socket; socket.create_connection(('127.0.0.1', 9))", {})
    assert "network is disabled in the python tool" in out, out
    out = run_python("print(PAGES['/a'], datetime.date(2026, 1, 2) + timedelta(days=1))", {"/a": "hello"})
    assert out.strip() == "hello 2026-01-03", out
    assert run_python("", {}) == "(no output)"
    out = run_python("print('first'); raise ValueError('x')", {})
    assert out.startswith("first") and "STDERR:" in out and "ValueError" in out, out
    assert os.environ["LLM_API_KEY"] == "sk-test"   # the agent itself keeps its key

    # REUSE-02/03/04: stored programs (format gate, answer equality, learn, confirm, fall back, discard on a message)
    config.LEARN_TRIES, config.PROGRAM_STEPS, config.BUDGET, config.LOOP_BUDGET_SHARE = 2, 4, 8.0, 0.9
    config.MIN_CONFIDENCE = 0.7
    assert checked_answer("How many charts are open?", "12") == ("12", "")
    v, why = checked_answer("How many charts are open?", "twelve")
    assert v is None and why.startswith("implausible"), why
    for bad in ("a\nb", "x" * 201, True, "", ["x"] * 51):
        v, why = checked_answer("Which code?", bad)
        assert v is None and why.startswith("bad format"), (bad, why)
    assert checked_answer("Which code?", "true")[0] is None
    assert checked_answer("Which code?", " 'NO_ACTION' ") == ("NO_ACTION", "")
    assert checked_answer("Which code?", ["B", "A"]) == (["B", "A"], "")
    assert same_answer("BILL_PATIENT : 40.00", "bill_patient:40.00") and same_answer(["B", "A"], ["a", "b"])
    assert same_answer("12", 12)
    assert not same_answer("12.5", "12.50") and not same_answer("A", ["A"])

    ITEM_PROGRAM = (
        "def step(task, pages, patients):\n"
        "    m = re.search(r'code of item (\\d+)', task)\n"
        "    if not m:\n"
        "        return {'skip': 'not an item code request'}\n"
        "    path = '/items/' + m.group(1)\n"
        "    if path not in pages:\n"
        "        return {'open': [path]}\n"
        "    v = re.search(r'Code: (\\d+)', pages[path].get('text') or '')\n"
        "    return {'answer': v.group(1)} if v else {'skip': 'no code'}\n")
    ONE_LINE = {"m7": "return {'answer': 'true'}", "m8": "return {'answer': 'a\\nb'}", "m9": "raise ValueError('boom')"}
    CODES = {"/items/7": "4417", "/items/8": "5521", "/items/9": "6630"}

    class ProgSite:
        cache = {}

        async def read_many(self, paths):
            return [{"path": p, "title": "Item", "text": f"Item {p[7:]}\nCode: {CODES[p]}", "tables": [], "links": []}
                    if p in CODES else {"path": p, "error": "page did not load"} for p in paths]

        async def read_document(self, path):
            return {"text": ""}

    class ProgLLM:
        spent, calls = 0.0, 0

        async def chat(self, messages, **kw):
            self.calls += 1
            return {"content": "```python\n" + ITEM_PROGRAM + "```", "_finish": "stop"}, {}, 0.1

    def item(n):
        return f"What is the code of item {n}?"
    buf = io.StringIO()

    async def programs():
        s = Solver(ProgSite(), ProgLLM(), Memory())
        loops = []

        async def no_fast(text):
            return None

        async def stub_loop(text, deadline):
            loops.append(text)
            return "skip", None, {"why": "stub loop", "route": []}
        s.fast.solve, s._loop = no_fast, stub_loop

        async def answered(label, n, value, info):   # a loop answer of label; waits for the job it starts
            s._after_answer(label, item(n), value, info)
            job = s.jobs.get(label)   # taken before awaiting: a finished job removes itself
            if job:
                await job
            return job
        # learn, then confirm on a task whose answer differs from the example's, then answer from the program
        assert await answered("m2", 7, "4417", {"convo": [], "route": ["/items/7"]})
        assert s.programs["m2"]["confirmed"] is False and "[program] m2 learned from " in buf.getvalue()
        assert await answered("m2", 8, "5521", {})
        assert s.programs["m2"]["confirmed"] is True and "[program] m2 confirmed on " in buf.getvalue()
        kind, value, info = await s.solve(item(9), time.time() + 10, allow_loop=False, label="m2")
        assert (kind, value) == ("answer", "6630"), (kind, value, info)
        assert info["why"] == "stored program m2" and info["route"][0] == "program:m2" and info["secs"] < 4, info
        # hard-code guard: agreeing on the example's own answer confirms nothing
        await answered("m4", 7, "4417", {"convo": []})
        await answered("m4", 7, "4417", {})
        assert s.programs["m4"]["confirmed"] is False and "not confirmed yet" in buf.getvalue()
        # disagreement with the model loop drops the program
        await answered("m5", 7, "4417", {"convo": []})
        await answered("m5", 8, "9999", {})
        assert "m5" not in s.programs and "[program] m5 discarded: it gave " in buf.getvalue()
        # a program that does not reproduce the loop's answer is not stored; LEARN_TRIES calls per label at most
        calls = s.llm.calls
        assert await answered("m6", 7, "1111", {"convo": []})
        assert "m6" not in s.programs and "[program] m6 not learned:" in buf.getvalue()
        assert await answered("m6", 7, "1111", {"convo": []})
        s._after_answer("m6", item(7), "1111", {"convo": []})   # the third try starts no job
        assert "m6" not in s.jobs
        assert s.llm.calls - calls == 2 and "m6" not in s.programs, s.llm.calls - calls
        # no learning without a rule-message kind, without a transcript, or at the loop budget reserve
        for label, info in (("unmatched", {"convo": []}), ("phone", {"convo": []}), ("m10", {})):
            s._after_answer(label, item(7), "4417", info)
            assert label not in s.jobs, label
        s.llm.spent = 0.9 * config.BUDGET
        s._after_answer("m11", item(7), "4417", {"convo": []})
        assert "m11" not in s.jobs
        s.llm.spent = 0.0
        # REUSE-03: a confirmed program's miss falls back to the loop and is never sent
        for label, body in ONE_LINE.items():
            s.programs[label] = {"code": "def step(task, pages, patients):\n    " + body + "\n", "answer": "x",
                                 "task": "t", "confirmed": True}
        for label, why in (("m7", "implausible"), ("m8", "bad format"), ("m9", "program failed")):
            n = len(loops)
            res = await s.solve(item(9), time.time() + 10, allow_loop=True, label=label)
            assert len(loops) == n + 1 and res == ("skip", None, {"why": "stub loop", "route": []}), (label, res)
            assert f"[program] {label} fallback to the model loop: {why}" in buf.getvalue(), label
        res = await s.solve(item(9), time.time() + 10, allow_loop=False, label="m7")
        assert res[0] == "skip" and res[1] is None, res
        # REUSE-04: a message that best matches rule message 3 discards m3 and m3/..., never m2 or m31
        correction = "Correction: " + claim[:400]
        s.rules.items = [vending, filing, claim]
        assert s.rules.kind_for(correction) == 3   # fixture precondition
        s.programs = {k: dict(s.programs["m2"]) for k in ("m3", "m3/claim_next_step", "m2", "m31")}
        sleeper = asyncio.create_task(asyncio.sleep(60))
        s.jobs["m3"], s.tries["m3"] = sleeper, 2
        s.add_message(correction)
        await asyncio.sleep(0)
        assert sorted(s.programs) == ["m2", "m31"], sorted(s.programs)
        assert sleeper.cancelled() and "m3" not in s.jobs and "m3" not in s.tries
        assert "[program] m3 discarded: message 4 best matches its rule message 3" in buf.getvalue()
        assert "[program] m3/claim_next_step discarded: message 4" in buf.getvalue()
        await answered("m3", 7, "4417", {"convo": []})   # tries were reset: the next loop answer learns again
        assert "m3" in s.programs and s.programs["m3"]["confirmed"] is False
        # empty input: a message matching no long rule message, or one before any, discards nothing
        before, lines = sorted(s.programs), buf.getvalue().count("discarded: message")
        s.add_message("zzz qqq")
        assert sorted(s.programs) == before and buf.getvalue().count("discarded: message") == lines
        s2 = Solver(ProgSite(), ProgLLM(), Memory())
        s2.programs["m1"] = dict(s.programs["m2"])
        s2.add_message(vending)
        assert list(s2.programs) == ["m1"] and buf.getvalue().count("discarded: message") == lines
    with contextlib.redirect_stdout(buf):
        asyncio.run(programs())

    # SAFE-03: the judge entry point names every missing URL variable and exits 1
    def require(**urls):
        return subprocess.run([sys.executable, "-c", "from agent import config; config.require_urls()"], cwd=ROOT,
                              env={"PATH": os.environ["PATH"], **urls}, capture_output=True, text=True)
    r = require(QUEUE_URL="http://q", SITE_URL="http://s")
    assert r.returncode == 1 and "LLM_BASE_URL" in r.stderr and "SITE_URL" not in r.stderr, r.stderr
    r = require()
    assert r.returncode == 1 and all(k in r.stderr for k in ("QUEUE_URL", "SITE_URL", "LLM_BASE_URL")), r.stderr
    r = require(QUEUE_URL="http://q", SITE_URL="http://s", LLM_BASE_URL="http://l")
    assert r.returncode == 0, r.stderr

    # SAFE-04: every answer passes the confidence gate (strict less-than; a missing conf skips)
    def gated(info):
        s = Solver(object(), object(), Memory())

        async def fixed(text):
            return "answer", "42", dict(info, route=[])
        s.fast.solve = fixed
        return asyncio.run(s.solve("t", time.time() + 5, allow_loop=False))
    res = gated({"conf": 0.5})
    assert res[0] == "skip" and "below confidence gate" in res[2]["why"], res
    assert gated({"conf": 0.9})[0] == "answer" and gated({"conf": 0.7})[0] == "answer"
    assert gated({})[0] == "skip"

    # Rule 1, browser: same scheme, host and port as SITE_URL only; data:, blob: and about: never leave the box
    config.SITE_URL = "http://site:8000"
    assert same_origin("http://site:8000/patients?x=1")
    assert same_origin("data:image/png;base64,AA")
    assert same_origin("blob:x")
    assert same_origin("about:blank")
    assert not same_origin("https://fonts.googleapis.com/css")
    assert not same_origin("http://site:80001/")
    assert not same_origin("https://site:8000/")
    assert not same_origin("http://site:8000.evil/")
    config.SITE_URL = "https://Site.Example:443"   # the browser reports https://site.example/...
    assert same_origin("https://site.example/x") and not same_origin("http://site.example/x")

    # DIAG-01: which startup stage failed ("" = OK, None = not known yet); location needs a majority of misses
    T, F = True, False
    assert startup_probe(T, T, 1, 0) == ""
    assert startup_probe(T, T, 0, 4) is None
    assert startup_probe(T, T, 0, 5) == "search"
    assert startup_probe(T, T, 1, 50) == ""
    assert startup_probe(F, T, 2, 0) == ""
    assert startup_probe(F, T, 0, 5) == "search"
    assert startup_probe(F, F, 0, 5) == "sign-in"
    assert startup_probe(T, F, 0, 0) is None
    assert startup_probe(T, F, 0, 4) is None
    assert startup_probe(T, F, 5, 0) == ""
    assert startup_probe(T, F, 3, 2) == ""
    assert startup_probe(F, F, 3, 3) == ""
    assert startup_probe(T, F, 2, 3) == "location"
    assert startup_probe(T, F, 3, 9) == "location"
    assert startup_probe(T, F, 0, 5) == "location"
    assert startup_probe(F, F, 2, 3) == "location"
    assert sorted(PROBE_CODES.values()) == [1, 2, 3], PROBE_CODES
    # decode contract at the default step 10: wrong // 10 is the stage code, wrong % 10 the normal wrongs
    assert all((10 * c + n) // 10 == c and (10 * c + n) % 10 == n for c in (1, 2, 3) for n in range(10))

    # ROB-02: section routes from the sidebar; translated both ways at the Site boundary
    config.SITE_URL = "http://site:8000"
    R = {"/patients": "/people", "/schedule": "/calendar", "/billing": "/invoices", "/documents": "/records"}
    assert learn_routes([["Patients", "/people"], ["Schedule", "/calendar?date=2026-10-14"], ["Billing", "/invoices/"],
                         ["Documents", "/records"]]) == R
    assert learn_routes([["Patients", "/patients"]]) == {"/patients": "/patients"}
    assert learn_routes([["Patients", "/people"], ["Patients", "/other"]]) == {"/patients": "/people"}
    assert learn_routes([[" patients ", "/people"]]) == {"/patients": "/people"}
    assert learn_routes([["Patients", "http://site:8000/people"]]) == {"/patients": "/people"}
    assert learn_routes([["Patients", "https://elsewhere.example/people"], ["Patients", "/"], ["Patients", "#main"],
                         ["Inbox", "/inbox"], ["Billing", "//cdn.example/x"]]) == {}
    assert to_site("/patients?dob=1976-04-08", R) == "/people?dob=1976-04-08"
    assert to_site("/patients/gp/documents", R) == "/people/gp/documents"
    assert to_site("/patients", R) == "/people"
    assert to_site("/patientsx", R) == "/patientsx"
    assert to_site("/v/1", R) == "/v/1"
    assert to_site("/patients/gp", {}) == "/patients/gp"
    assert from_site("/people/gp", R) == "/patients/gp"
    assert from_site("/records/d1", R) == "/documents/d1"
    assert from_site("/peoplex", R) == "/peoplex"
    assert from_site("/people?dob=1", R) == "/patients?dob=1"
    S = {"/documents": "/billing", "/billing": "/documents"}
    assert to_site("/documents/x", S) == "/billing/x"
    for p in ("/documents/x", "/billing?patient=A", "/patients/1"):
        assert from_site(to_site(p, S), S) == p, p
    print("all logic checks passed")


if __name__ == "__main__":
    main()
