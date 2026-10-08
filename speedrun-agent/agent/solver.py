"""One task = one short LLM tool loop over the Wrenfield pages."""
import asyncio
import json
import os
import re
import subprocess
import sys
import time

from . import clinic, config
from .fast import Fast
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
    pages_file = os.path.join(config.WORK_DIR, "pages.json")
    with open(pages_file, "w") as f:
        json.dump(pages, f)
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

    def add_message(self, text):
        self.rules.add(text)

    def system_prompt(self, task_text):
        msgs = self.rules.for_task(task_text)
        loc = "All locations" if getattr(self.site, "location_ok", False) else (
            "NOT confirmed as All locations: the patient search may show only one location's patients. If a patient "
            "is not found, look for them in /documents?patient=<last name> and /billing?patient=<last name>, whose rows "
            "lead to the chart")
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

    async def solve(self, task_text, deadline, allow_loop=True):
        """Returns (kind, value, info). kind is 'answer' or 'skip'. Known kinds go the deterministic way first;
        the model loop gets whatever time is left."""
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
        if not allow_loop:
            return "skip", None, {"why": "loop not allowed (hopeless kind, time short)", "route": []}
        if self.llm.spent >= config.BUDGET * config.LOOP_BUDGET_SHARE:
            return "skip", None, {"why": "model budget reserve", "route": []}
        kind, value, info = await self._loop(task_text, deadline)
        if kind == "answer" and not plausible(task_text, value):
            kind, info = "skip", {**info, "why": f"implausible answer {value!r}"[:150]}
        self.memory.outcome(task_text, kind == "answer")
        return confidence_gate(kind, value, info)

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
                    return "answer", ans, info
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
