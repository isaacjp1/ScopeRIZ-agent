"""Shift memory, two jobs.

Route hints: each solved task stores the sequence of pages that led to the answer. A new task that looks like an
earlier one gets that route as a hint, so the model can jump straight to the right pages instead of exploring again.

Kind records: per kind (the rule message a task matches, REUSE-01) the answers and the skips in a row so far. They
decide each task's time (allot): a kind that keeps failing while time is short is written off and skipped in about
SKIP_SECONDS, with a longer retry now and then (TIME-01).
"""
import re

from . import config

STOP = set("""a an the of for to in on at by with and or is was are be do does did what which who whom
how many much when where our we patient patients please""".split())


def signature(text):
    """Words that describe the kind of task, without names, numbers and dates."""
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*|\S", text)
    sig = set()
    for i, w in enumerate(words):
        if not w[0].isalpha():
            continue
        if w[0].isupper() and i > 0 and words[i - 1] not in (".", "?", ":", "!"):
            continue  # likely a name
        lw = w.lower()
        if lw in STOP or len(lw) < 3:
            continue
        sig.add(lw)
    return sig


class Memory:
    def __init__(self):
        self.items = []     # dicts: text, sig, route, answer
        self.kinds = {}     # kind label -> [answered, skips in a row, written-off tasks]
        self.tasks = 0      # tasks allotted so far
        self.written_off = 0   # of those, how many were written off (retries not counted)

    def add(self, text, route, answer):
        self.items.append({"text": text, "sig": signature(text), "route": route[:12], "answer": answer})

    def similar(self, text, k=2, threshold=0.4):
        s = signature(text)
        if not s:
            return []
        scored = []
        for it in self.items:
            inter = len(s & it["sig"])
            union = len(s | it["sig"]) or 1
            j = inter / union
            if j >= threshold:
                scored.append((j, it))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], set()
        for j, it in scored:
            key = tuple(it["route"])
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
            if len(out) >= k:
                break
        return out

    def hint(self, text):
        sims = self.similar(text)
        if not sims:
            return ""
        parts = ["Earlier tasks of the same kind and the pages that answered them (copy the route, with this task's names and dates):"]
        for it in sims:
            route = " -> ".join(it["route"]) or "(no pages)"
            parts.append(f"- Task: {it['text'][:300]}\n  Route: {route}\n  Answer sent: {it['answer']}")
        return "\n".join(parts)

    # ---- per-kind records: only the main shift loop reads and writes them, between queue items ----
    def outcome(self, kind, answered):
        rec = self.kinds.setdefault(kind, [0, 0, 0])
        if answered:
            rec[0] += 1
            rec[1] = 0
        else:
            rec[1] += 1

    def allot(self, kind, secs_left, tasks_left):
        """(mode, seconds) for the next task of this kind, checked in this order:
        - time short (fair share < TIGHT_FAIR_SECONDS) and HOPELESS_AFTER+ skips in a row: 'retry' (EXPLORE_SECONDS,
          model loop) on every EXPLORE_EVERY-th such task when the time left covers it, else 'written-off'
          (SKIP_SECONDS, deterministic paths only);
        - answered before: 'known', the larger of task_cap and task_cap with the written-off share so far (TIME-02);
        - else 'new', task_cap."""
        share = self.written_off / self.tasks if self.tasks else 0.0
        self.tasks += 1
        rec = self.kinds.setdefault(kind, [0, 0, 0])
        if secs_left / max(1, tasks_left) < config.TIGHT_FAIR_SECONDS and rec[1] >= config.HOPELESS_AFTER:
            rec[2] += 1
            if config.EXPLORE_EVERY > 0 and rec[2] % config.EXPLORE_EVERY == 0 \
                    and secs_left - config.EXPLORE_SECONDS > tasks_left * config.SKIP_SECONDS:
                return "retry", config.EXPLORE_SECONDS
            self.written_off += 1
            return "written-off", config.SKIP_SECONDS
        if rec[0] > 0:
            return "known", max(config.task_cap(secs_left, tasks_left), config.task_cap(secs_left, tasks_left, share))
        return "new", config.task_cap(secs_left, tasks_left)
