"""Clinic messages. Each one is acknowledged right away and kept word for word, in order. In the background the
new-vs-established rule is compiled into parameters for the deterministic path and checked against the message text.
A later message on the same point overrides an earlier one: compiling always reads every rule message so far."""
import asyncio
import json
import math
import re

from .clinic import norm

NE_RE = re.compile(r"\bestablished\b|\bnew patients?\b", re.I)

COMPILE = """The clinic messages below (in order; a later one overrides an earlier one) define how to decide whether a \
visit is a NEW or an ESTABLISHED patient visit. Extract the CURRENT rule. Reply with JSON only:
{"years": <look-back years, integer>,
 "counting_statuses": [<appointment statuses that count, copied exactly, e.g. "Completed">],
 "excluded_services": [<services that never count, each copied exactly>],
 "same_specialty_counts": <true if an appointment with a different provider of the same specialty counts>,
 "all_locations": <true if appointments at every location count>,
 "window": <"standard" if the window starts the day after the same date N years before the visit and ends the day before the visit, else "other">,
 "new_answer": <the exact answer when nothing counts>,
 "established_example": <one example established answer copied exactly from the messages, e.g. "ESTABLISHED:2025-03-11">,
 "other_conditions": [<any OTHER condition that changes which appointments count or the answer. Do NOT list: status, \
provider/specialty, excluded services, the window, every location, merged duplicate records, each date of a recurring \
appointment with its own status, the calendar date at the location, inactive providers keeping their specialty. Empty list if none>]}

MESSAGES:
"""


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def check_ne(spec, text):
    """Return usable parameters, or None if anything is unexpected (then the general loop handles these tasks)."""
    if not isinstance(spec, dict):
        return None
    low = " " + norm(text) + " "
    try:
        years = int(spec.get("years"))
        statuses = [str(s) for s in spec.get("counting_statuses") or []]
        excluded = [str(s) for s in spec.get("excluded_services") or []]
        new = str(spec.get("new_answer") or "").strip()
        example = str(spec.get("established_example") or "").strip()
    except (TypeError, ValueError):
        return None
    m = re.fullmatch(r"(.*?)(\d{4}-\d{2}-\d{2})", example)
    ok = (1 <= years <= 10 and statuses and new and m and m.group(1)
          and spec.get("same_specialty_counts") is True and spec.get("all_locations") is True
          and spec.get("window") == "standard" and not spec.get("other_conditions")
          and all(" " + norm(s) + " " in low for s in statuses + excluded + [new])
          and example.replace(" ", "") in text.replace(" ", ""))
    if not ok:
        return None
    return {"years": years, "statuses": statuses, "excluded": excluded, "new": new, "prefix": m.group(1)}


def _list(s):
    return [x.strip(" .`'\"") for x in re.split(r",\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+", s) if x.strip(" .`'\"")]


def parse_ne_text(text):
    """The rule in its standard wording, read without a model call (the compile call can wait a long time for a
    request slot). None unless every part is found and the 'counts only if' list has exactly the four standard
    conditions; anything else is left to the compile."""
    one = re.sub(r"\s+", " ", text)
    years = re.search(r"\bin the (\d+) years? before the visit date\b", one, re.I)
    status = re.search(r"\bIts status is ([A-Za-z ,\-]+?)\.", one)
    excl = re.search(r"\bnever make a patient established: ([^.]+)\.", one, re.I)
    new = re.search(r"\bIf nothing counts, answer `?([A-Z_]+)`?", one)
    est = re.search(r"\bOtherwise answer `?([A-Z_]+ ?:)`? followed by the date", one)
    example = re.search(r"`?([A-Z_]+ ?:\d{4}-\d{2}-\d{2})`?", one)
    m = re.search(r"counts only if all of these are true:(.*?)(?:\bLook at all\b|^The \d+-year window\b)", text, re.S | re.M)
    if not (years and status and excl and new and est and example and m):
        return None
    conds = [ln.strip(" -•*\t") for ln in m.group(1).split("\n") if ln.strip(" -•*\t")]
    standard = (r"^Its status is ", r"^It was with the same provider or with a provider in the same specialty\b",
                r"^It is not one of these services\b", r"^Its date is inside the \d+-year window\.?$")
    if len(conds) != 4 or not all(any(re.match(p, c) for c in conds) for p in standard):
        return None
    if not (re.search(r"every one of our locations", one) and re.search(r"starts the day after the same date", one)):
        return None
    return check_ne({"years": int(years.group(1)), "counting_statuses": _list(status.group(1)),
                     "excluded_services": _list(excl.group(1)), "same_specialty_counts": True, "all_locations": True,
                     "window": "standard", "new_answer": new.group(1), "established_example": example.group(1),
                     "other_conditions": []}, text)


class Rules:
    def __init__(self, llm):
        self.llm = llm
        self.items = []          # message texts, in order
        self.ne = None           # checked new-vs-established parameters, or None
        self._ne_task = None
        self._ne_version = 0     # only the newest compile may set self.ne (a later message overrides)

    def add(self, text):
        self.items.append(text)
        if NE_RE.search(text):
            self._ne_version += 1
            # One rule message in its standard wording: usable at once. Otherwise never use the old rule while the
            # new one compiles. The compile still runs and can veto the text parse.
            ne_msgs = [t for t in self.items if NE_RE.search(t)]
            self.ne = parse_ne_text(text) if len(ne_msgs) == 1 else None
            if self.ne:
                print(f"[rules] new/established rule read from the text {json.dumps(self.ne)}", flush=True)
            self._ne_task = asyncio.create_task(self._compile_ne(self._ne_version))

    async def _compile_ne(self, version):
        text = "\n\n".join(f"[Message {i + 1}]\n{t}" for i, t in enumerate(self.items) if NE_RE.search(t))
        spec = None
        for attempt in range(2):   # a reply cut off by max_tokens (finish "length") is never used
            try:
                msg, _, _ = await self.llm.chat([{"role": "user", "content": COMPILE + text}], max_tokens=4000, timeout=40)
                if msg.get("_finish") != "length":
                    spec = parse_json(msg.get("content"))
            except Exception as e:
                print(f"[rules] compile failed: {e!r}"[:200], flush=True)
            if spec:
                break
        if version != self._ne_version:
            return
        if spec is None and self.ne:
            print("[rules] compile gave nothing; keeping the rule read from the text", flush=True)
            return
        compiled = check_ne(spec, text)
        if self.ne and compiled != self.ne:
            print(f"[rules] compile disagrees with the text parse, using the compile: {json.dumps(compiled)}", flush=True)
        self.ne = compiled
        print(f"[rules] new/established rule {'OK ' + json.dumps(self.ne) if self.ne else 'NOT usable: ' + json.dumps(spec)[:400]}",
              flush=True)

    async def ne_rule(self, wait=15):
        if self._ne_task and not self._ne_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._ne_task), wait)
            except Exception:
                return None
        return self.ne

    def _ranked(self, task_text):
        """(score, index) of every long rule message (1200+ chars) for this task, best first. Each shared word of 4+
        letters weighs log(1 + n/df); a tie goes to the higher index, the newer message."""
        toks = [set(re.findall(r"[a-z]{4,}", t.lower())) for t in self.items]
        n = len(toks)
        df = {}
        for ts in toks:
            for w in ts:
                df[w] = df.get(w, 0) + 1
        words = set(re.findall(r"[a-z]{4,}", task_text.lower()))
        longs = [i for i, t in enumerate(self.items) if len(t) >= 1200]
        return sorted(((sum(math.log(1 + n / df[w]) for w in words & toks[i]), i) for i in longs), reverse=True)

    def kind_for(self, task_text):
        """REUSE-01: the kind of a task is the rule message it best matches, as its 1-based number, or 0 for none.
        Short messages (facts, chatter, rule changes) never become kinds."""
        scored = self._ranked(task_text)
        return scored[0][1] + 1 if scored and scored[0][0] > 0 else 0

    def for_task(self, task_text, top=2, short_chars=4000):
        """The messages a model-loop prompt needs: the long rule messages that best fit the task (words weighted by
        how few messages use them, so 'patient' counts for little and 'tetanus' for a lot) plus the latest short
        messages (facts, exceptions, changes to a rule), in the order received. Falls back to relevant() when no rule
        shares a distinctive word with the task."""
        if not self.items:
            return "(none yet)"
        scored = self._ranked(task_text)
        longs = {i for _, i in scored}
        if not scored or scored[0][0] <= 0:
            return self.relevant(task_text)
        keep = {i for s, i in scored[:top] if s >= scored[0][0] * 0.5}
        budget = short_chars
        for i in range(len(self.items) - 1, -1, -1):     # newest short messages first
            if i not in longs and len(self.items[i]) <= budget:
                keep.add(i)
                budget -= len(self.items[i])
        return "\n\n".join(f"[Message {i + 1}]\n{self.items[i]}" for i in sorted(keep))

    def relevant(self, task_text, max_chars=16000):
        """Messages for a prompt, in order. If too long, keep those sharing the most words with the task."""
        keep = list(enumerate(self.items))
        if sum(len(t) for _, t in keep) > max_chars:
            words = set(re.findall(r"[a-z]{4,}", task_text.lower()))
            ranked = sorted(keep, key=lambda x: (-len(words & set(re.findall(r"[a-z]{4,}", x[1].lower()))), -x[0]))
            out, n = [], 0
            for i, t in ranked:
                if n + len(t) <= max_chars:
                    out.append((i, t))
                    n += len(t)
            keep = sorted(out)
        return "\n\n".join(f"[Message {i + 1}]\n{t}" for i, t in keep) or "(none yet)"
