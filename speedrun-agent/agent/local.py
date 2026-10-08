"""Run the solver on your own machine against the hosted practice clinic, without the judge.

    python -m agent.local items.json

items.json is a list of {"kind": "message"|"task", "text": "...", "expected": "..."} in queue order.
Copy items and expected answers from a practice run record (arena runs <run id>).
Needs LLM_API_KEY (your development key) in the environment.
"""
import asyncio
import json
import os
import sys
import time

# Local runs target the hosted practice clinic; the judge entry point (agent.main) requires all three URL variables.
os.environ.setdefault("SITE_URL", "https://practice.speedrun.scopehealth.com")
os.environ.setdefault("LLM_BASE_URL", "https://openrouter.ai/api/v1")

from . import config
from .llm import LLM
from .memory import Memory
from .site import Site
from .solver import Solver


def norm(v):
    if isinstance(v, list):
        return sorted(str(x).strip().lower() for x in v)
    return " ".join(str(v).lower().replace(" :", ":").replace(": ", ":").split())


async def run(path):
    items = json.load(open(path))
    site, llm, memory = Site(), LLM(), Memory()
    solver = Solver(site, llm, memory)
    await asyncio.gather(site.start(), llm.pick_model())
    score = 0
    for it in items:
        if it["kind"] == "message":
            solver.add_message(it["text"])
            continue
        t0 = time.time()
        kind, value, info = await solver.solve(it["text"], time.time() + config.TASK_SECONDS)
        exp = it.get("expected")
        verdict = "skip"
        if kind == "answer":
            ok = exp is not None and norm(value) == norm(exp)
            verdict = "RIGHT" if ok else "WRONG?"
            score += 1 if ok else -1
            memory.add(it["text"], info.get("route", []), value)
        print(f"{verdict:6} {time.time() - t0:5.1f}s got={value!r} expected={exp!r} | {info.get('why', '')} "
              f"conf={info.get('conf', '')}\n       route={info.get('route')}\n       task={it['text'][:140]!r}", flush=True)
    print(f"score ~{score} (text compare only; the judge is more lenient on numbers, dates and phones) "
          f"spent ${llm.spent:.3f}")
    await site.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "practice_items.json"))
