"""One task = one short LLM tool loop over the Wrenfield pages."""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from . import clinic, config
from .fast import DOB_RE, Fast
from .llm import BudgetExhausted
from .rules import Rules

NOTES = open(os.path.join(os.path.dirname(__file__), "notes.md")).read()
EXTRA_NOTES = os.environ.get("AGENT_NOTES", "")

SYSTEM = """You are a fast, careful back-office assistant at a medical clinic. You answer one request at a time using \
ONLY the clinic's records system, Wrenfield, which you read through the tools. You never change any data.

SCORING: correct answer +1, wrong answer -1, skip 0. Answer only when the records clearly support the answer; \
otherwise call skip_task. Time is scarce (a few seconds per task is ideal): use the fastest tool, open several \
pages in ONE open_pages call when you can, and do not browse around.

FAST TOOLS (prefer these over browsing)
- find_patient(last_name, first_name, dob): exact chart lookup. Returns every matching chart with its path and \
flags (Merged = a duplicate record that was merged into another chart; Deleted; Restricted). A task written \
"Henry, Peter" means last name Henry, first name Peter. If the user message already lists patients born on the \
task's date of birth, use that list instead of calling it.
- appointment_history(patient_paths): every appointment of a patient (Appointments tab + each date of every \
recurring series with that date's status), dates already converted to the location's calendar date.
- new_or_established(patient_paths, provider, visit_date, excluded_services, years, counting_statuses): applies \
a "new vs established patient" rule exactly (window from the day after the same date N years earlier through \
the day before the visit; same provider or same specialty; counting statuses; excluded services). Fill the \
parameters from the CURRENT rule in the clinic messages (copy the excluded services list exactly). If \
safe_to_answer is false, read its warnings: fix what you can (e.g. open the chart) or skip.
- read_document(path): the text of a document (referral letter, lab report, statement) from its file. Document \
lists show only metadata; open the document's link (e.g. /documents/<id>) with read_document to read content.
- provider_info(name): a provider's specialty, id (for the appointment list filter) and whether they are active or inactive (left the group).
- patient_contact(patient_path): the chart's Phone / Email / Address AS SHOWN ON SCREEN. PHONE DIGITS IN THE PAGE \
TEXT ARE SCRAMBLED (the screen is correct), so for ANY phone number question call this and answer its "phone" \
exactly; if safe_to_answer is false, skip. Never copy a phone number from open_pages text.

HOW TO WORK
- open_pages takes site paths like /patients?lastName=Singh. Build URLs from the links you see. Tables are shown \
as "## header | ..." then one row per line with that row's links after "->". Use find="word|other" to see only \
matching lines of a long page.
- Identify the right record: match every detail the task gives (first and last name, date of birth, date, provider). \
Several patients can share a name; never pick a chart whose date of birth differs from the task's.
- Use the python tool for date arithmetic, counting or summing over many rows. In python, PAGES is a dict \
{path: full page text} of every page opened so far.
- "Today" is the clinic date given below, not the real-world date.
- Contact details: answer with exactly what the chart's current Demographics/Contact shows (use patient_contact).
- If a page says the chart is restricted, you cannot read it: skip unless the answer is available elsewhere.
- Rule-based questions: gather ALL inputs first, apply the rule step by step, then answer in the exact form the \
rule gives.

ANSWER FORMAT (answers are machine-compared)
- If a message defines the answer form for this kind of request, follow it character for character as ONE string \
(case and spaces around ':' and ',' are ignored; leading zeros, decimals, '$', order are not).
- Otherwise give just the value, no words: whole numbers bare (12, not "12 patients"); amounts like 12.50; \
dates YYYY-MM-DD; date-times "YYYY-MM-DD HH:MM" in the time zone the question names; phone numbers as shown; \
names, addresses, IDs copied exactly as the product shows them, in the order the question asks \
("Jane Smith" vs "Smith, Jane" matters; "St" vs "Street" matters). Lists in the product are "Last, First": \
if the question asks for a patient's name without saying the order, use the form the product shows.
- For a question asking for several items, pass a JSON list of strings.
- Never answer true/false; never answer an empty string.

{notes}
{extra}

<<DYNAMIC>>
CLINIC TODAY: {today}. Working location: {location}.

MESSAGES FROM THE CLINIC (rules, facts and workflows, in the order received; a later message overrides an earlier \
one on the same point; some messages are unrelated chatter):
{messages}"""

TOOLS = [
    {"type": "function", "function": {
        "name": "open_pages",
        "description": "Open one or more Wrenfield pages (in parallel) and return their text, links and forms.",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Site paths, e.g. /patients?lastName=Ng"},
            "find": {"type": "string", "description": "Optional: only show lines containing any of these |-separated words"},
        }, "required": ["paths"]}}},
    {"type": "function", "function": {
        "name": "find_patient",
        "description": "Find patient charts by last name and/or first name and/or date of birth (YYYY-MM-DD). Returns matching charts with path, MRN, DOB and flags.",
        "parameters": {"type": "object", "properties": {
            "last_name": {"type": "string"}, "first_name": {"type": "string"},
            "dob": {"type": "string", "description": "Date of birth, YYYY-MM-DD"}}}}},
    {"type": "function", "function": {
        "name": "appointment_history",
        "description": "All appointments of a patient (chart list + every date of recurring series, with status), newest first.",
        "parameters": {"type": "object", "properties": {
            "patient_paths": {"type": "array", "items": {"type": "string"}, "description": "Chart paths like /patients/abc123 (include merged duplicates if separate)"}},
            "required": ["patient_paths"]}}},
    {"type": "function", "function": {
        "name": "new_or_established",
        "description": "Apply a new-vs-established patient rule exactly and return the result with the evidence.",
        "parameters": {"type": "object", "properties": {
            "patient_paths": {"type": "array", "items": {"type": "string"}},
            "provider": {"type": "string", "description": "Provider of the visit, as written, e.g. 'Logan Brown, MD'"},
            "visit_date": {"type": "string", "description": "YYYY-MM-DD"},
            "excluded_services": {"type": "array", "items": {"type": "string"}, "description": "Services that never count, copied from the rule"},
            "years": {"type": "integer", "description": "Look-back years from the rule (e.g. 3)"},
            "counting_statuses": {"type": "array", "items": {"type": "string"}, "description": "Statuses that count, e.g. [\"Completed\"]"}},
            "required": ["patient_paths", "provider", "visit_date", "excluded_services", "years"]}}},
    {"type": "function", "function": {
        "name": "read_document",
        "description": "Read the text of a document (its PDF file) from the document's page, e.g. /documents/<id>.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "provider_info",
        "description": "A provider's specialty and status (Active/Inactive).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "patient_contact",
        "description": "Phone, email and address of a chart as displayed on screen (page-text phone digits are scrambled).",
        "parameters": {"type": "object", "properties": {"patient_path": {"type": "string", "description": "/patients/<id>"}},
                       "required": ["patient_path"]}}},
    {"type": "function", "function": {
        "name": "look_at_page",
        "description": "Screenshot a page's visual content (a scanned letter, PDF or image) so you can read it.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "python",
        "description": "Run Python 3 code and return what it prints. PAGES = {path: text} of opened pages. datetime is imported.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "submit_answer",
        "description": "Submit the final answer for this task.",
        "parameters": {"type": "object", "properties": {
            "answer": {"description": "A string, a number, or a list of strings for list questions"},
            "confidence": {"type": "number", "description": "Probability (0 to 1) that the answer is exactly right"},
        }, "required": ["answer", "confidence"]}}},
    {"type": "function", "function": {
        "name": "skip_task",
        "description": "Skip this task (scores 0) when the records do not clearly give the answer.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
]


# Prepended to the python tool's code: Rule 1 allows only the three judge addresses, and model-written code needs none.
NO_NET = ("import socket\n"
          "def _no_net(*a, **k):\n    raise OSError('network is disabled in the python tool')\n"
          "class _NoNet(socket.socket):\n    def __init__(self, *a, **k):\n        _no_net()\n"
          "socket.socket = _NoNet\nsocket.create_connection = _no_net\nsocket.getaddrinfo = _no_net\n")


def run_python(code, pages, timeout=6):
    # A file per call: background stored-program runs and the loop's python tool can overlap (REUSE-02).
    fd, pages_file = tempfile.mkstemp(suffix=".json", dir=config.WORK_DIR)
    with os.fdopen(fd, "w") as f:
        json.dump(pages, f, default=str)
    prelude = ("import json, re, math, datetime\nfrom datetime import date, timedelta\n"
               f"PAGES = json.load(open({pages_file!r}))\n")
    try:
        # -I and an env holding only PATH: the child never sees LLM_API_KEY or SITE_PASSWORD; the parent keeps both.
        # limit: import _socket still opens a raw socket; Rule 1 hygiene for a non-hostile model, the judge network refuses every other address
        r = subprocess.run([sys.executable, "-I", "-c", NO_NET + prelude + code], capture_output=True, text=True,
                           timeout=timeout, env={"PATH": os.environ.get("PATH", "")})
        out = (r.stdout + ("\nSTDERR: " + r.stderr[-1500:] if r.returncode else ""))
    except subprocess.TimeoutExpired:
        out = "timeout"
    finally:
        os.remove(pages_file)
    return out[:4000] or "(no output)"


def clean_answer(a):
    if isinstance(a, str):
        a = a.strip()
        if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'`":
            a = a[1:-1].strip()
        if a.startswith("[") and a.endswith("]"):
            try:
                v = json.loads(a)
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        return a
    if isinstance(a, bool):
        return None
    if isinstance(a, (int, float)):
        return a
    if isinstance(a, list):
        return [str(x).strip() if not isinstance(x, (int, float)) else x for x in a if str(x).strip()]
    return None


def plausible(task, answer):
    """Cheap shape checks on a model-loop answer. A wrong answer costs a point, so an odd shape becomes a skip."""
    t = task.lower()
    vals = answer if isinstance(answer, list) else [answer]
    s = " ".join(str(v) for v in vals).strip()
    if not s or re.fullmatch(r"(?i)(true|false|none|null|n/?a|unknown|not found)", s):
        return False
    if re.search(r"\bhow many\b", t) and not isinstance(answer, list):
        return bool(re.fullmatch(r"\$?-?[\d,]+(\.\d+)?%?", s))
    if re.search(r"\bwhat\b[^?]{0,30}\bphone\b", t) and not isinstance(answer, list):
        return len(re.sub(r"\D", "", s)) in (10, 11)
    return True


def checked_answer(task, value):
    """REUSE-03: the format and plausibility gate a stored program's answer passes before it is sent. Format: after
    clean_answer, a non-empty one-line string of at most 200 characters, a number, or a list of 1-50 such items.
    Returns (value, "") or (None, why)."""
    v = clean_answer(value)
    items = v if isinstance(v, list) else [v]
    if v is None or v == "" or v == [] or len(items) > 50 or any(
            isinstance(x, str) and ("\n" in x or len(x) > 200) for x in items):
        return None, f"bad format {value!r}"[:150]
    if not plausible(task, v):
        return None, f"implausible answer {v!r}"[:150]
    return v, ""


def same_answer(a, b):
    """The judge's text comparison for message-defined answer forms: case, whitespace runs and the spaces around ':'
    and ',' do not matter. A list compares as its sorted items; a list never equals a non-list. Numbers compare by
    their text, so 12 equals "12" and "12.5" does not equal "12.50"."""
    def key(x):
        return re.sub(r"\s*([:,])\s*", r"\1", re.sub(r"\s+", " ", str(x).strip().lower()))
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and sorted(map(key, a)) == sorted(map(key, b))
    return key(a) == key(b)


# REUSE-02: one model call per kind writes a program from a loop-solved task; filled in one pass by _learn.
LEARN = """Write a Python program that answers one kind of clinic request from the Wrenfield records system, so that \
later requests of this kind are answered without a model.

Requests of this kind are phrased in many ways and name other patients, dates, providers and amounts. Below are the \
clinic messages (the rule for this kind), one request an assistant solved, the answer it sent, the patients list for \
that request, the assistant's transcript and samples of the page data it read.

CLINIC MESSAGES:
{rules}

SOLVED REQUEST:
{task}

ANSWER SENT: {answer}

PATIENTS LIST FOR THAT REQUEST:
{patients}

TRANSCRIPT:
{transcript}

PAGE DATA SAMPLES (what pages[...] holds for a site path):
{pages}

THE PROGRAM
Define step(task, pages, patients). It is called again after each "open", with the pages it asked for added, at most \
{steps} calls in all. Return ONE dict:
- {"open": ["/site/path?x=y", "doc:/documents/<id>", ...]}: read these pages. pages[request] then holds each page, \
keyed by the exact string you asked for. "doc:<document page path>" reads that document's file text into \
pages["doc:<document page path>"]["text"].
- {"answer": value}: the answer, in exactly the answer form the messages give (a string, a number, or a list of \
strings for list questions).
- {"skip": "why"}: when the request is not this kind, an input is missing or ambiguous, or the pages do not clearly \
give the answer.
A wrong answer costs a point and a skip costs nothing, so skip whenever in doubt.

A page is a dict {"path", "title", "text", "tables": [{"headers": [...], "rows": [{"cells": [...], "links": [[text, \
href], ...]}]}], "links": [[text, href], ...]}, or {"path", "error"} when it did not load. patients is a list of \
{"name": "Last, First", "mrn", "dob", "flags": [...], "path": "/patients/<id>"}: the charts born on the request's date \
of birth, or [] when it gives none. flags may hold Merged, Deleted, Restricted or Test record.

Read every name, date, id and amount from task and the pages; never copy one from the solved request. Follow the \
same pages and checks the assistant used. Only json, re, math and datetime are available (already imported), with \
no network, no files and no printing.

Reply with the program only, in one python code block."""


PROBE_CODES = {"location": 1, "search": 2, "sign-in": 3}


def startup_probe(signed_in, location_ok, hits, misses, need=5):
    """Which startup stage failed: "" = startup OK, None = not known yet. Set 2/3 show no logs, so a failure is sent
    as PROBE * code certainly-wrong answers in place of skips, and wrong // PROBE in the tally names the stage."""
    if hits > 0 and location_ok:
        return ""          # one hit settles a clinic whose All-locations switch is confirmed
    if hits + misses < need:
        return None
    if location_ok:
        return "search"    # hits is 0 here, so misses >= need
    if signed_in or hits > 0:
        # _location_is_all is False on a clinic with one location or no "All locations" option, so an unconfirmed
        # switch alone is not a failure: only a majority of misses among the first `need` distinct date-of-birth
        # searches reports it, and a tie reports nothing, because a probe on a healthy clinic is pure lost points
        # (DIAG-01: when startup succeeds the probe never fires).
        return "location" if misses > hits else ""
    return "sign-in"


def confidence_gate(kind, value, info):
    """Every answer, deterministic or model, is sent only at MIN_CONFIDENCE or above; a missing conf counts as 0.
    Strict less-than, like the loop's own check, so exactly MIN_CONFIDENCE is still sent."""
    if kind != "answer":
        return kind, value, info
    conf = float(info.get("conf") or 0)
    if conf < config.MIN_CONFIDENCE:
        return "skip", None, {**info, "why": f"below confidence gate {conf}"}
    return kind, value, info


class Solver:
    def __init__(self, site, llm, memory):
        self.site = site
        self.llm = llm
        self.memory = memory
        self.rules = Rules(llm)      # clinic messages, in order
        self.fast = Fast(site, llm, self.rules)
        self.programs = {}   # REUSE-02: label -> {"code", "answer" (its example's), "task", "confirmed"}
        self.jobs = {}       # label -> the asyncio task learning or checking that label's program
        self.tries = {}      # label -> learning calls made (at most LEARN_TRIES)

    def add_message(self, text):
        """REUSE-04: a new or changed rule message, or a fact or chatter that best matches a kind, discards that kind's
        stored programs and stops their jobs. The match is taken before the message is added, since a long message
        would match itself. Over-discarding only costs a relearn."""
        n = self.rules.kind_for(text)
        self.rules.add(text)
        if not n:
            return
        k = len(self.rules.items)
        for label in [x for x in {*self.programs, *self.jobs, *self.tries} if x == f"m{n}" or x.startswith(f"m{n}/")]:
            prog, job = self.programs.pop(label, None), self.jobs.pop(label, None)
            self.tries.pop(label, None)
            if job:
                job.cancel()
            if prog or job:
                print(f"[program] {label} discarded: message {k} best matches its rule message {n}", flush=True)

    def system_prompt(self, task_text):
        msgs = self.rules.for_task(task_text)
        loc = "All locations" if getattr(self.site, "location_ok", False) else (
            "NOT confirmed as All locations: the patient search may show only one location's patients. If a patient "
            "is not found, look for them in /documents?patient=<last name> and /billing?patient=<last name>, whose rows "
            "lead to the chart")
        if clinic.search_dead(self.site):
            loc += (" The patient search found nobody for the first 5 dates of birth: find patients through "
                    "/documents?patient=<last name> and /billing?patient=<last name>, whose rows lead to the chart.")
        return (SYSTEM.replace("{notes}", NOTES).replace("{extra}", EXTRA_NOTES)
                .replace("{today}", getattr(self.site, "today", "") or "see the dashboard at /")
                .replace("{location}", loc).replace("{messages}", msgs))

    async def prefetch(self, task_text):
        """Patients born on the task's date of birth, looked up before the first model call (saves a turn)."""
        m = re.search(r"(?:born|DOB|date of birth|birth date|b\.)\s*:?\s*(?:on\s+)?(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2},? \d{4})",
                      task_text, re.I)
        if not m:
            return ""
        try:
            res = await asyncio.wait_for(clinic.find_patient(self.site, dob=m.group(1)), 6)
        except Exception:
            return ""
        rows = res.get("matches") or []
        if not rows:
            return f"Prefetched: no patient found born {m.group(1)} (searched {', '.join(res.get('searched', []))})."
        lines = [f"- {r['name']} | MRN {r['mrn']} | DOB {r['dob']} | {' '.join(r['flags']) or '-'} -> {r['path']}" for r in rows[:15]]
        return f"Prefetched: patients born {m.group(1)} (all locations):\n" + "\n".join(lines)

    async def _patients(self, task_text):
        """A stored program's patients input: find_patient matches for the task's date of birth, [] for none."""
        m = DOB_RE.search(task_text)
        try:
            d = clinic.parse_any_date(m.group(1)) if m else None
            if not d:
                return []
            return (await clinic.find_patient(self.site, dob=d.isoformat())).get("matches") or []
        except Exception:
            return []

    async def run_program(self, code, task_text, deadline):
        """Runs a stored program (REUSE-02) -> (value or None, why, route). Its text runs only in run_python (no
        secrets, sockets blocked), and it acts on the clinic only by reading pages (Site.read_many) and document text
        (Site.read_document): no clicks, forms or POSTs."""
        patients = await self._patients(task_text)
        driver = (f"\n_r = step(json.loads({json.dumps(task_text)!r}), PAGES, "
                  f"json.loads({json.dumps(patients, default=str)!r}))\nprint('RESULT:' + json.dumps(_r, default=str))\n")
        pages, route = {}, []
        for _ in range(config.PROGRAM_STEPS):
            left = deadline - time.time()
            if left < 0.5:
                return None, "time", route
            out = await asyncio.to_thread(run_python, code + driver, pages, max(0.5, min(6, left)))
            lines = [ln for ln in out.splitlines() if ln.startswith("RESULT:")]
            try:
                res = json.loads(lines[-1][len("RESULT:"):]) if lines else None
            except ValueError:
                res = None
            if not isinstance(res, dict):
                return None, "program failed: " + out[-150:], route
            if res.get("answer") is not None:
                return res["answer"], "", route
            if "open" in res:
                asked = res["open"] if isinstance(res["open"], list) else []
                want = [r for r in asked if isinstance(r, str) and r not in pages][:8]
                if not want:
                    return None, "program asked for nothing new", route
                docs = [r for r in want if r.startswith("doc:")]
                paths = [r for r in want if not r.startswith("doc:")]
                got = await asyncio.gather(self.site.read_many(paths),
                                           *[self.site.read_document(r[len("doc:"):]) for r in docs])
                pages.update(zip(paths, got[0]))
                pages.update(zip(docs, got[1:]))
                route += want
                continue
            if "skip" in res:
                return None, "program skipped: " + str(res["skip"])[:100], route
            return None, "program gave no open, answer or skip", route
        return None, f"program used all {config.PROGRAM_STEPS} steps", route

    def _after_answer(self, label, task_text, value, info):
        """A model-loop answer starts a job for its kind label (REUSE-02): learning when the label has no program,
        checking when its program is unconfirmed. Only rule-message kinds ("m<N>", "m<N>/<path>"): "unmatched" and bare
        path names pool unrelated requests."""
        if not re.match(r"^m\d+(/|$)", label or "") or label in self.jobs:
            return
        prog = self.programs.get(label)
        if prog is None:
            if "convo" not in info or self.tries.get(label, 0) >= config.LEARN_TRIES \
                    or self.llm.spent >= config.BUDGET * config.LOOP_BUDGET_SHARE:
                return
            self.tries[label] = self.tries.get(label, 0) + 1
            job = self._learn(label, task_text, value, info)
        elif not prog["confirmed"]:
            job = self._check(label, task_text, value)
        else:
            return
        self.jobs[label] = asyncio.create_task(job)

    async def _learn(self, label, task_text, value, info):
        """One model call writes a program from this loop-solved task. It is kept, unconfirmed, only if it gives the
        loop's answer on this same task."""
        why = ""
        try:
            parts = []
            for m in info["convo"][1:]:
                if m.get("tool_calls"):
                    s = "assistant: " + " ; ".join(f"{c['function']['name']}({c['function'].get('arguments', '')})"
                                                   for c in m["tool_calls"])
                else:
                    s = f"{m['role']}: {m.get('content')}"
                parts.append(s[:1500])
            cache, samples = getattr(self.site, "cache", {}), []
            for r in info.get("route", []):
                p = cache.get(r) if isinstance(r, str) and r.startswith("/") else None
                if isinstance(p, dict):
                    samples.append({"path": r, "title": p.get("title"), "text": (p.get("text") or "")[:1500],
                                    "tables": [{"headers": t.get("headers"), "rows": (t.get("rows") or [])[:5]}
                                               for t in (p.get("tables") or [])[:3]],
                                    "links": (p.get("links") or [])[:15]})
            vals = {"rules": self.rules.for_task(task_text), "task": task_text, "answer": json.dumps(value),
                    "patients": json.dumps(await self._patients(task_text), default=str),
                    "transcript": "\n".join(parts)[:12000], "pages": json.dumps(samples, default=str)[:12000],
                    "steps": str(config.PROGRAM_STEPS)}
            # one pass, so a placeholder written inside a message or a page is never filled in
            prompt = re.sub(r"\{(rules|task|answer|patients|transcript|pages|steps)\}", lambda x: vals[x.group(1)], LEARN)
            msg, _, _ = await self.llm.chat([{"role": "user", "content": prompt}], max_tokens=6000, timeout=90)
            text = msg.get("content") or ""
            block = re.search(r"```(?:python)?[ \t]*\n(.*?)```", text, re.S)
            code = block.group(1) if block else text
            t1 = time.time()
            if msg.get("_finish") == "length":
                why = "the reply was cut off"
            elif "def step(" not in code:
                why = "no step() in the reply"
            else:
                got, why, route = await self.run_program(code, task_text, time.time() + 30)
                if got is not None:
                    got, why = checked_answer(task_text, got)
                if got is not None and not same_answer(got, value):
                    why = f"it gave {got!r} where the model loop answered {value!r}"
                elif got is not None:
                    self.programs[label] = {"code": code, "answer": value, "task": task_text, "confirmed": False}
                    print(f"[program] {label} learned from {task_text[:80]!r}: reproduces {value!r} in "
                          f"{time.time() - t1:.1f}s, {len(route)} request(s)", flush=True)
                    return
        except Exception as e:   # asyncio.CancelledError is not an Exception: a discarding message still cancels
            why = f"{e!r}"
        finally:
            if self.jobs.get(label) is asyncio.current_task():
                del self.jobs[label]
        print(f"[program] {label} not learned: {why[:200]}", flush=True)

    async def _check(self, label, task_text, value):
        """Runs an unconfirmed program on a later loop answer of its kind. It is confirmed when it gives the loop's
        answer and that answer differs from its example's, which a program hard-coding its example cannot do."""
        # limit: a confirmed program that keeps falling back on one phrasing costs its run time on every such task;
        # relearn from the loop answer after a fallback if the Set 1 log shows many fallback lines.
        try:
            prog = self.programs.get(label)
            if not prog:
                return
            got, why, _ = await self.run_program(prog["code"], task_text, time.time() + 20)
            if got is not None:
                got, why = checked_answer(task_text, got)
            if got is None:
                print(f"[program] {label} not checked on {task_text[:60]!r}: {why}"[:300], flush=True)
            elif same_answer(got, value) and not same_answer(got, prog["answer"]):
                prog["confirmed"] = True
                print(f"[program] {label} confirmed on {task_text[:80]!r}: {got!r} agrees with the model loop", flush=True)
            elif same_answer(got, value):
                print(f"[program] {label} agrees on {task_text[:60]!r} but with the example's own answer: "
                      "not confirmed yet", flush=True)
            else:
                self.programs.pop(label, None)
                print(f"[program] {label} discarded: it gave {got!r} where the model loop answered {value!r}"[:300],
                      flush=True)
        except Exception as e:
            print(f"[program] {label} not checked on {task_text[:60]!r}: {e!r}"[:300], flush=True)
        finally:
            if self.jobs.get(label) is asyncio.current_task():
                del self.jobs[label]

    async def solve(self, task_text, deadline, allow_loop=True, label=""):
        """Returns (kind, value, info). kind is 'answer' or 'skip'. Known kinds go the deterministic way first, then
        the kind label's confirmed stored program (REUSE-03); the model loop gets whatever time is left."""
        t0 = time.time()
        try:
            res = await asyncio.wait_for(self.fast.solve(task_text), max(1.0, deadline - time.time() - 1))
        except BudgetExhausted:
            raise
        except Exception as e:
            print(f"[fast] {e!r}"[:200], flush=True)
            res = None
        if res:
            res[2]["secs"] = round(time.time() - t0, 1)
            return confidence_gate(*res)
        prog = self.programs.get(label)
        if prog and prog["confirmed"]:
            value, why, route = await self.run_program(prog["code"], task_text, deadline)
            if value is not None:
                value, why = checked_answer(task_text, value)
            if value is not None:
                return confidence_gate("answer", value, {"why": f"stored program {label}", "conf": 1.0,
                                                         "route": ["program:" + label] + route,
                                                         "secs": round(time.time() - t0, 1)})
            print(f"[program] {label} fallback to the model loop: {why}"[:300], flush=True)
        if not allow_loop:
            return "skip", None, {"why": "loop not allowed (hopeless kind, time short)", "route": []}
        if self.llm.spent >= config.BUDGET * config.LOOP_BUDGET_SHARE:
            return "skip", None, {"why": "model budget reserve", "route": []}
        kind, value, info = await self._loop(task_text, deadline)
        if kind == "answer" and not plausible(task_text, value):
            kind, info = "skip", {**info, "why": f"implausible answer {value!r}"[:150]}
        res = confidence_gate(kind, value, info)
        if res[0] == "answer":
            self._after_answer(label, task_text, res[1], info)
        return res

    async def _loop(self, task_text, deadline):
        t0 = time.time()
        hint = self.memory.hint(task_text)
        pre = await self.prefetch(task_text)
        user = f"TASK:\n{task_text}"
        if pre:
            user += "\n\n" + pre
        if hint:
            user += "\n\n" + hint
        convo = [{"role": "system", "content": self.system_prompt(task_text)},
                 {"role": "user", "content": user}]
        route = []
        opened_text = {}
        for turn in range(config.MAX_TURNS):
            left = deadline - time.time()
            if left < 2:
                return "skip", None, {"why": "time", "route": route}
            self._compact(convo)
            try:
                msg, usage, dt = await asyncio.wait_for(self.llm.chat(convo, TOOLS, timeout=max(5, left)), left)
            except BudgetExhausted:
                raise
            except Exception as e:
                return "skip", None, {"why": f"llm {e!r}"[:120], "route": route}
            calls = msg.get("tool_calls") or []
            convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                         else {"role": "assistant", "content": msg.get("content") or ""})
            if not calls:
                convo.append({"role": "user", "content": "Use a tool (open_pages, find_patient, read_document, python, ...), or finish with submit_answer or skip_task."})
                continue
            images = []
            if True:   # always: one line per model turn (scored-set output is the only real evaluation data)
                note = (msg.get("content") or "").strip().replace("\n", " ")[:300]
                print(f"[debug] turn {turn + 1} {dt:.1f}s tokens={usage.get('prompt_tokens')}/{usage.get('completion_tokens')} "
                      + " ; ".join(f"{c['function']['name']}({c['function'].get('arguments', '')[:200]})" for c in calls)
                      + (f" | says: {note}" if note else ""), flush=True)
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                if name == "submit_answer":
                    ans = clean_answer(args.get("answer"))
                    conf = float(args.get("confidence") or 0)
                    info = {"route": route, "conf": conf, "turns": turn + 1, "secs": round(time.time() - t0, 1)}
                    if ans in (None, "", []):
                        return "skip", None, {**info, "why": "empty"}
                    if conf < config.MIN_CONFIDENCE:
                        return "skip", ans, {**info, "why": f"low confidence {conf}"}
                    return "answer", ans, {**info, "convo": convo}   # the learning prompt is built from it
                if name == "skip_task":
                    return "skip", None, {"why": str(args.get("reason"))[:150], "route": route, "turns": turn + 1}
                if name == "open_pages":
                    paths = [p for p in (args.get("paths") or []) if isinstance(p, str)][:8]
                    pages = await self.site.read_many(paths)
                    self.site.learn_nav(pages)
                    for p in pages:
                        if not p.get("error"):
                            opened_text[p["path"]] = p["text"]
                            route.append(p["path"])
                    result = "\n\n".join(self.site.render(p, find=args.get("find")) for p in pages) or "no paths given"
                    if config.DEBUG:
                        for p in pages:
                            print("[page] " + self.site.render(p, max_chars=2500).replace("\n", "\n[page] ")[:4000], flush=True)
                elif name in ("find_patient", "appointment_history", "new_or_established", "read_document", "provider_info",
                              "patient_contact"):
                    try:
                        res = await asyncio.wait_for(self._fast_tool(name, args), max(3, deadline - time.time()))
                    except asyncio.TimeoutError:
                        res = {"error": "timed out"}
                    except Exception as e:
                        res = {"error": f"{e!r}"[:300]}
                    route.append(f"{name}:{json.dumps(args, ensure_ascii=False)[:120]}")
                    ne = self.rules.ne
                    if name == "new_or_established" and ne and isinstance(res, dict) and res.get("safe_to_answer"):
                        # Checked rule + safe result: the answer is fixed, so submit now instead of re-checking.
                        last = res.get("most_recent_counting_date")
                        return "answer", (ne["prefix"] + last if last else ne["new"]), {
                            "route": route, "conf": 1.0, "turns": turn + 1, "secs": round(time.time() - t0, 1),
                            "why": "new_or_established safe"}
                    result = json.dumps(res, ensure_ascii=False, default=str)[:config.PAGE_CHARS]
                    if name == "read_document" and isinstance(res, dict) and res.get("text"):
                        opened_text["doc:" + str(args.get("path"))] = res["text"]
                    print(f"[tool] {name} -> {result[:1500 if config.DEBUG else 400]}", flush=True)
                elif name == "look_at_page":
                    shots = await self.site.look(str(args.get("path", "/")))
                    route.append("look:" + str(args.get("path")))
                    images.extend(shots)
                    result = f"{len(shots)} screenshot(s) attached in the next message." if shots else "no visual content found"
                elif name == "python":
                    result = await asyncio.to_thread(run_python, args.get("code", ""), opened_text)
                else:
                    result = f"unknown tool {name}"
                convo.append({"role": "tool", "tool_call_id": c.get("id", name), "content": result})
            if images:
                convo.append({"role": "user", "content": [{"type": "text", "text": "Screenshots you asked for:"}] +
                              [{"type": "image_url", "image_url": {"url": u}} for u in images]})
        return "skip", None, {"why": "max turns", "route": route}

    async def _fast_tool(self, name, a):
        site = self.site
        if name == "find_patient":
            return await clinic.find_patient(site, a.get("last_name"), a.get("first_name"), a.get("dob"))
        if name == "appointment_history":
            res = await clinic.appointment_history(site, a.get("patient_paths") or [])
            res["appointments"] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()
                                    if k in ("date", "time", "type", "provider", "location", "status", "source")}
                                   for r in res["appointments"][:120]]
            return res
        if name == "new_or_established":
            ne = self.rules.ne   # the checked rule wins over what the model copied
            return await clinic.new_or_established(
                site, a.get("patient_paths") or [], a.get("provider", ""), a.get("visit_date", ""),
                ne["excluded"] if ne else a.get("excluded_services") or [], ne["years"] if ne else a.get("years") or 3,
                ne["statuses"] if ne else a.get("counting_statuses") or ["Completed"])
        if name == "patient_contact":
            return await clinic.patient_contact(site, self.llm, str(a.get("patient_path", "")))
        if name == "read_document":
            res = await site.read_document(str(a.get("path", "")))
            if res.get("text"):
                res["text"] = res["text"][:6000]
            return res
        if name == "provider_info":
            provs = await site.providers()
            spec = await site.provider_specialty(a.get("name", ""))
            from .site import norm_name
            key = norm_name(a.get("name", ""))
            m = [p for p in provs if norm_name(p["name"]) == key or norm_name(p["name"].split(",")[0]) == norm_name(key.split(",")[0])]
            out = {"name": a.get("name"), "specialty": spec,
                   "matches": [{"name": p["name"], "status": p["group"] or "?", "id": p["value"].rsplit("/", 1)[-1]} for p in m]}
            if not spec and len(m) == 1 and m[0]["value"]:
                v = m[0]["value"]
                out["page"] = v if v.startswith("/") else f"/schedule/providers/{v}"
                out["hint"] = "specialty not read; open this page: the line under the name reads '<Specialty> · <date>'"
            return out
        return {"error": "unknown tool"}

    @staticmethod
    def _compact(convo, keep_last=2, limit=2500):
        """Shorten old tool outputs to keep each call fast and cheap."""
        tool_idx = [i for i, m in enumerate(convo) if m["role"] == "tool"]
        for i in tool_idx[:-keep_last] if len(tool_idx) > keep_last else []:
            c = convo[i]["content"]
            if len(c) > limit:
                convo[i]["content"] = c[:limit] + "\n...[older output shortened]"
