"""Deterministic paths for task kinds we know. No model turns for browsing: regex picks the kind, the records pick
the patient and provider (a name only counts if the records confirm it), code computes the answer.
Each path returns ("answer", value, info) or None. None means "not sure": the general model loop takes over,
with every page already read sitting in the cache."""
import re

from . import clinic
from .clinic import norm, parse_any_date
from .rules import parse_json

DOB_RE = re.compile(r"(?:born|DOB|date of birth|birth ?date|b\.)\s*:?\s*(?:on\s+)?"
                    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})", re.I)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\b[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}")
NE_TASK = re.compile(r"\bnew\b.{0,40}\bestablished\b|\bestablished\b.{0,40}\bnew\b", re.I | re.S)
PHONE_TASK = re.compile(r"\bphone\b", re.I)
PHONE_OTHER = re.compile(r"referr|practice|pharmac|emergency|insur|employer|provider|doctor|\bwork\b|mobile|cell|fax|"
                         r"guarantor|kin\b|contact person|location|office|clinic|caller|previous|\bold\b|former|changed|"
                         r"how many|which|list|digits?\b", re.I)
DOC_TASK = re.compile(r"\b(referral letter|referral|lab report|statement|letter|document|report)\b.{0,40}?"
                      r"\b(received|dated|from|filed|uploaded|on)\b", re.I | re.S)
DOC_KINDS = ("referral", "lab", "statement")

DOC_Q = """Answer the question using ONLY the document below. The answer must be text copied exactly from the \
document (no added words, no rewording). If the document does not clearly answer it, answer null.
Clinic instructions that may set the answer form:
{rules}

QUESTION: {task}

DOCUMENT ({title}):
{text}

Reply with JSON only: {{"answer": <copied text or null>, "confidence": <0 to 1>}}"""

DOC_IMG_Q = """The image is a clinic document ({title}). Transcribe it, then answer the question using ONLY the \
document. The answer must be words copied exactly from the document (no added words, no rewording). If the document \
does not clearly answer it, answer null.
Clinic instructions that may set the answer form:
{rules}

QUESTION: {task}

Reply with JSON only: {{"letter_text": <the full document text, transcribed exactly>, "answer": <copied text or null>, \
"confidence": <0 to 1>}}"""

NOT_NAMES = set("""what which who whom how is are was does did do when where why can could please patient patients
referral letter lab report statement billing question phone number born dob date birth the for our we visit new
established on in at of and with dr doctor md do np pa invoice balance""".split())
WHO_REFERRED = re.compile(r"which (practice|clinic|office|group|organi[sz]ation|facility)|who (made|sent|wrote|referred)|"
                          r"referred by|from which|what practice", re.I)


def dates_in(text):
    out = []
    for s in DATE_RE.findall(text):
        d = parse_any_date(s)
        if d and d not in out:
            out.append(d)
    return out


def _has(words, text):
    return bool(words) and f" {words} " in text


async def pick_patient(site, task, without=()):
    """The one chart born on the task's DOB whose first and last name both appear in the task. (row, dob) or (None, why)."""
    m = DOB_RE.search(task)
    dob = parse_any_date(m.group(1)) if m else None
    if not dob:
        return None, "no date of birth"
    res = await clinic.find_patient(site, dob=dob.isoformat())
    if res.get("notes") and any("could be read" in n for n in res["notes"]):
        return None, "patient list incomplete"
    if not getattr(site, "location_ok", True) or clinic.search_dead(site):
        # The patient search shows only the working location's patients, or finds nobody at all: add charts found
        # through the lists that cover every location, for each capitalized word of the task that could be the last name.
        # limit: up to 6 words x 2 list reads per task in degraded mode; cache the lists per last name if a Set 1 log shows this eating the task cap
        seen = {r["path"] for r in res.get("matches", [])}
        words = [w for w in dict.fromkeys(re.findall(r"\b[A-Z][a-zA-Z'\u00C0-\u017F-]{1,}\b", task))
                 if w.lower() not in NOT_NAMES]
        for w in words[:6]:
            for r in await clinic.find_patient_anywhere(site, w, dob.isoformat()):
                if r["path"] not in seen:
                    seen.add(r["path"])
                    res.setdefault("matches", []).append(r)
    text = task
    for w in without:
        text = re.sub(re.escape(w), " ", text, flags=re.I)
    text = " " + norm(text) + " "

    def named(matches):
        out = []
        for r in matches:
            last, _, first = r["name"].partition(",")
            if _has(norm(last), text) and _has(norm(first).split(" ")[0] if norm(first) else "", text):
                out.append(r)
        return out
    rows = named(res.get("matches", []))
    if not rows:
        # the search by date of birth found no chart with the task's names (or the clinic's search has no DOB
        # field): search each word that could be the last name, keeping charts born on the task's date
        words = [w for w in dict.fromkeys(re.findall(r"\b[A-Z][a-zA-Z'\u00C0-\u017F-]{1,}\b", task))
                 if w.lower() not in NOT_NAMES and not any(w.lower() in x.lower().split() for x in without)]
        for w in words[:5]:
            more = await clinic.find_patient(site, last_name=w, dob=dob.isoformat())
            if more.get("notes") and any("could be read" in n for n in more["notes"]):
                return None, "patient list incomplete"
            rows = named(more.get("matches", []))
            if rows:
                break
    rows = [r for r in rows if "Test record" not in r["flags"]]
    merged = [r for r in rows if "Merged" in r["flags"]]
    active = [r for r in rows if not {"Merged", "Deleted"} & set(r["flags"])] or merged   # a merged link opens the survivor
    if len(active) != 1:
        return None, f"{len(active)} matching charts"
    if "Restricted" in active[0]["flags"]:
        return None, "restricted chart"
    # the duplicates merged into it, so nothing filed under them is missed
    return {**active[0], "paths": list(dict.fromkeys([active[0]["path"]] + [r["path"] for r in merged]))}, dob


def kind_of(task):
    """Name of the deterministic path that takes this task, "" for none; also the label of the per-kind summary."""
    from . import solvers   # Set 1 kinds (appointment counts, open slots, filing deadlines, claim next step); circular
    for match, solve in solvers.KINDS:
        if match.search(task):
            return solve.__name__
    if NE_TASK.search(task):
        return "new_established"
    if PHONE_TASK.search(task) and not PHONE_OTHER.search(task):
        return "phone"
    if DOC_TASK.search(task):
        return "document"
    return ""


def no(why):
    print(f"[fast] fallback to the model loop: {why}", flush=True)
    return None


class Fast:
    def __init__(self, site, llm, rules):
        self.site, self.llm, self.rules = site, llm, rules

    async def solve(self, task):
        from . import solvers   # circular import, as in kind_of
        label = kind_of(task)   # the dispatch order lives in kind_of only
        if not label:
            return None
        for _, solve in solvers.KINDS:
            if solve.__name__ == label:
                return await solve(self, task)
        return await getattr(self, label)(task)   # new_established, phone, document

    async def new_established(self, task):
        rule = await self.rules.ne_rule()
        if not rule:
            return no("no checked new/established rule")
        provs = await self.site.providers()
        low = " " + norm(task) + " "
        found = {}
        for p in provs:
            full, bare = norm(p["name"]), norm(p["name"].split(",")[0])
            if _has(full, low) or _has(bare, low):
                found[bare] = p["name"]
        if len(found) != 1:
            return no(f"{len(found)} providers named in the task")
        provider = next(iter(found.values()))
        pat, dob = await pick_patient(self.site, task, without=[provider, provider.split(",")[0]])
        if not pat:
            return no(dob)
        visit = [d for d in dates_in(task) if d != dob]
        if len(visit) != 1:
            return no(f"{len(visit)} visit dates in the task")
        res = await clinic.new_or_established(self.site, pat["paths"], provider, visit[0].isoformat(),
                                              rule["excluded"], rule["years"], rule["statuses"])
        if not res.get("safe_to_answer"):
            return no(f"new/established not safe: {res.get('warnings')}")
        last = res.get("most_recent_counting_date")
        answer = rule["prefix"] + last if last else rule["new"]
        return "answer", answer, {"route": ["fast:new_established", pat["path"]], "conf": 1.0,
                                  "why": f"{provider} {res.get('provider_specialty')} window {res.get('window')}"}

    async def phone(self, task):
        pat, why = await pick_patient(self.site, task)
        if not pat:
            return no(why)
        res = await clinic.patient_contact(self.site, self.llm, pat["path"])
        if not res.get("safe_to_answer") or len(re.sub(r"\D", "", res.get("phone") or "")) not in (10, 11):
            return no("phone screen read not safe")
        return "answer", res["phone"], {"route": ["fast:phone", pat["path"]], "conf": 1.0, "why": str(res.get("checks"))}

    async def document(self, task):
        pat, dob = await pick_patient(self.site, task)
        if not pat:
            return no(dob)
        when = [d for d in dates_in(task) if d != dob]
        if len(when) != 1:
            return no(f"{len(when)} document dates in the task")
        kinds = [k for k in DOC_KINDS if k in task.lower()]
        data = await self.site.read(pat["path"] + "/documents")
        if data.get("error") or data.get("incomplete"):
            return no("documents tab not read in full")
        rows = []
        for t in data.get("tables", []):
            for r in t["rows"]:
                line = " | ".join(r["cells"])
                docs = [h for _, h in r["links"] if "/documents/" in h]
                if docs and when[0] in [parse_any_date(c) for c in r["cells"]] and all(k in line.lower() for k in kinds):
                    rows.append((line, docs[0]))
        if len(rows) != 1:
            sample = [(t.get("headers"), [(r["cells"], [h for _, h in r["links"]][:2]) for r in t["rows"][:3]])
                      for t in data.get("tables", [])[:2]]
            print(f"[docs-debug] {pat['path']}/documents want={when[0]} kinds={kinds} tables={sample}"[:900], flush=True)
            return no(f"{len(rows)} documents match the date and kind")
        line, path = rows[0]
        # "Which practice made the referral?": the list row's title says "Referral letter from <practice>".
        if WHO_REFERRED.search(task):
            m = re.search(r"(?:^| \| )referral letter from (.+?)(?: \||$)", line, re.I)
            if m:
                return "answer", m.group(1).strip(), {"route": ["fast:document-title", pat["path"] + "/documents"], "conf": 1.0}
        doc = await self.site.read_document(path)
        text = (doc.get("text") or "").strip()
        rules = self.rules.relevant(task, 4000)
        if text:
            content = [{"type": "text", "text": DOC_Q.format(rules=rules, task=task, title=line, text=text[:6000])}]
        else:   # scanned letter: the model reads the page image and must transcribe it, so the answer can be checked
            shots = await self.site.look(path)
            if not shots:
                return no("no document text or image")
            content = [{"type": "text", "text": DOC_IMG_Q.format(rules=rules, task=task, title=line)}] + \
                      [{"type": "image_url", "image_url": {"url": u}} for u in shots[:2]]
        msg, _, _ = await self.llm.chat([{"role": "user", "content": content}], max_tokens=4000, timeout=30)
        if msg.get("_finish") == "length":
            return no("document reply cut off")
        out = parse_json(msg.get("content")) or {}
        ans = out.get("answer")
        if not isinstance(ans, str) or not ans.strip() or float(out.get("confidence") or 0) < 0.8:
            return no(f"no confident answer in the document: {out}"[:200])
        ans = ans.strip().strip('"').strip()
        source = text or str(out.get("letter_text") or "")
        if not text and len(norm(ans).split()) < 2:
            return no("one-word answer read off an image")
        # grounding check: the answer must be in the document (or its transcription) or its list row, word for word
        if " " + norm(ans) + " " not in " " + norm(source + " " + line) + " ":
            return no(f"answer not found word for word in the document: {ans!r}")
        return "answer", ans, {"route": ["fast:document", pat["path"] + "/documents", path], "conf": out.get("confidence")}
