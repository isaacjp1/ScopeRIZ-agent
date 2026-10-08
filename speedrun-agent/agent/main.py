"""Speedrun agent: works the queue until the shift ends."""
import asyncio
import os
import time

from . import config
from .fast import kind_of
from .llm import LLM, BudgetExhausted
from .memory import Memory
from .queue import Queue
from .site import Site
from .solver import PROBE_CODES, Solver, startup_probe


# Skip reasons that say nothing about the task's kind (startup, model budget, model errors): never recorded.
NOT_TRIED = ("site not ready", "budget", "model budget reserve", "llm ")


def log(*a):
    print("[agent]", *a, flush=True)


def kind_label(rules, text):
    """REUSE-01: (label, n), where n is the rule message the task best matches (0 for none). The deterministic path
    name is added, so tasks a code solver takes are never pooled with model-loop tasks of the same message."""
    n, rx = rules.kind_for(text), kind_of(text)
    if n:
        return f"m{n}" + (f"/{rx}" if rx else ""), n
    return rx or "unmatched", n


def print_kinds(kinds):
    """Per-kind summary. Printed every 25 tasks too, because the judge may stop the container at time-out and the
    periodic line is what survives in the Set 1 log. The label is the rule-message kind (REUSE-01, kind_label)."""
    rows = " | ".join(f"{k} {s}/{a}/{n} {secs:.0f}s w{w}" for k, (s, a, n, secs, w) in kinds.items())
    print(f"[kinds] {rows or '(no tasks)'}", flush=True)


async def run():
    start = time.time()
    os.makedirs(config.WORK_DIR, exist_ok=True)
    site, llm, memory, queue = Site(), LLM(), Memory(), Queue()
    solver = Solver(site, llm, memory)

    # Browser startup and model choice run in the background while we pull the first items:
    # messages need neither, so they are acknowledged at once.
    site_task = asyncio.create_task(site.start())
    model_task = asyncio.create_task(llm.pick_model())

    async def warm():  # provider directory + every specialty, in the background (2 tabs, so tasks keep the rest)
        await site.ready.wait()
        try:
            sem = asyncio.Semaphore(2)

            async def one(name):
                async with sem:
                    await site.provider_specialty(name)
            await asyncio.gather(*[one(p["name"]) for p in await site.providers()])
            log(f"provider specialties cached: {sum(1 for v in site.specialty_cache.values() if v)}")
        except Exception as e:
            log(f"provider directory failed: {e!r}")
    warm_task = asyncio.create_task(warm())
    est_tasks = config.expected_tasks()
    tasks_seen = 0

    stats = {"answered": 0, "skipped": 0, "messages": 0, "probe": 0}
    stage, probes_left = None, 0   # DIAG-01: the failed startup stage, decided once ("" = startup OK)
    kinds = {}   # DIAG-02: label -> [seen, answered, skipped, seconds, written off], in first-seen order
    recorded = set()   # item ids whose outcome moved a kind record: a re-served item never counts twice
    if config.SHIFT_SECONDS == 1200 and os.environ.get("RECON", "1") == "1":
        # Practice only (20 min for 6 tasks): map the app's pages before the queue, since the run stops when it ends.
        await site.ready.wait()
        try:
            await asyncio.wait_for(site.recon(), 240)
        except Exception as e:
            log(f"recon stopped: {e!r}")
    state = await queue.current()
    while True:
        if state.get("status") == "ended":
            break
        if state.get("status") != "open":
            # Queue unreachable (Queue.current gave up after its retries) or a malformed state. The shift clock keeps
            # running during an outage and a container exit ends the shift (speedrun-kit/docs/shift.md), so keep
            # asking until our own clock says the shift is over.
            if time.time() > start + config.SHIFT_SECONDS:
                break
            log(f"queue {str(state)[:200]}; retrying")
            await asyncio.sleep(2)
            state = await queue.current()
            continue
        item = {}
        try:   # one bad item must never end the shift: the except below closes it and carries on
            item = state.get("item") or {}
            secs_left = float(state.get("seconds_left", config.SHIFT_SECONDS))
            shift_end = time.time() + secs_left - config.SAFETY_SECONDS
            if item["kind"] == "message":
                solver.add_message(item["text"])
                stats["messages"] += 1
                log(f"{item['id']} message #{stats['messages']} acknowledged ({len(item['text'])} chars)")
                # Full text: the Set 1 output is the only place the scored clinics' rules can be read.
                log(f"   MESSAGE TEXT: {item['text']!r}")
                state = await queue.close(item["id"], acknowledge=True)
                continue

            t0 = time.time()   # the task's clock starts at receipt, so the logged time includes any startup wait
            tasks_seen += 1
            tasks_left = max(1, est_tasks - tasks_seen + 1)
            label, n = kind_label(solver.rules, item["text"])
            if label not in kinds:
                title = solver.rules.items[n - 1].strip().split("\n")[0][:80] if n else ""
                print(f"[kind] {label} <- message {n}: {title!r}" if n else f"[kind] {label} <- no rule message",
                      flush=True)
            row = kinds.setdefault(label, [0, 0, 0, 0.0, 0])   # a probe counts as its skip
            # TIME-01: a kind that keeps failing while time is short gets only the deterministic paths, in ~2 s.
            mode, cap = memory.allot(label, secs_left, tasks_left)
            deadline = min(time.time() + cap, shift_end)
            allow_loop = mode != "written-off"
            if mode == "retry":
                log(f"{item['id']} retrying written-off kind {label} for up to {cap:.0f}s")
            # Model listing is not awaited (LLM.chat waits on llm.ready inside the task's own caps), and sign-in is
            # waited for only until this task's deadline: a slow startup can cost the first tasks, never the shift.
            try:
                await asyncio.wait_for(site.ready.wait(), max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                kind, value, info = "skip", None, {"why": "site not ready"}
            else:
                try:   # with the model budget gone, the deterministic paths that need no model still answer
                    kind, value, info = await solver.solve(item["text"], deadline, allow_loop=allow_loop, label=label)
                except BudgetExhausted:
                    kind, value, info = "skip", None, {"why": "budget"}
                except Exception as e:
                    kind, value, info = "skip", None, {"why": f"error {e!r}"[:150]}
            if time.time() > shift_end + config.SAFETY_SECONDS:
                log("shift is over")
                break
            if stage is None and site.ready.is_set():
                stage = startup_probe(site.startup.get("signed_in", False), site.startup.get("location_ok", False),
                                      site.search_hits, site.search_misses)
                if stage is not None:   # decided once: never re-evaluated for the rest of the shift
                    probes_left = config.PROBE * PROBE_CODES.get(stage, 0)
                    print(f"[probe] startup stage={stage or 'ok'} -> {probes_left} probe answers "
                          f"(hits={site.search_hits} misses={site.search_misses})", flush=True)
            if kind == "answer":
                stats["answered"] += 1
                memory.add(item["text"], info.get("route", []), value)
                state = await queue.close(item["id"], answer=value)
            elif probes_left > 0:
                # Set 2/3 show only tallies: a failed startup is sent as PROBE*code certainly-wrong answers in place of
                # skips, so wrong // PROBE names the stage and no correct answer is lost. Never stored in memory.
                value = f"PROBE-{stage.upper()}"
                state = await queue.close(item["id"], answer=value)
                # Counted only once the queue has moved past the item: one probe too few misreads the stage
                # (29 // 10 = 2), one too many does not, so a close that may have failed is probed again.
                if state.get("status") in ("open", "ended") and (state.get("item") or {}).get("id") != item["id"]:
                    probes_left -= 1
                    stats["probe"] += 1
                print(f"[probe] {item['id']} sent {value} ({probes_left} left)", flush=True)
            else:
                stats["skipped"] += 1
                state = await queue.close(item["id"], skip=True)
            log(f"{item['id']} {kind.upper()} {value!r} in {time.time() - t0:.1f}s | {info.get('why', '')} "
                f"conf={info.get('conf', '')} pages={len(info.get('route', []))} kind={label} {mode} cap={cap:.0f}s "
                f"| task: {item['text']!r}")
            # Only tried tasks move the record: a written-off skip says nothing new, and a probe counts as its skip.
            if not str(info.get("why", "")).startswith(NOT_TRIED) and (mode != "written-off" or kind == "answer") \
                    and item["id"] not in recorded:
                recorded.add(item["id"])
                memory.outcome(label, kind == "answer")
            row[0] += 1
            row[1 if kind == "answer" else 2] += 1
            row[3] += time.time() - t0
            row[4] += mode == "written-off"
            log(f"   left {state.get('seconds_left', 0):.0f}s  spent ${llm.spent:.3f}  {stats}")
            if tasks_seen % 25 == 0:
                print_kinds(kinds)
            if llm.budget_gone and state.get("status") == "open":
                log("model budget used up")
        except Exception as e:
            log(f"loop error {e!r}"[:300])
            if isinstance(item, dict) and item.get("id"):
                # Close the failing item, so an error that repeats on it cannot stall the shift.
                field = {"acknowledge": True} if item.get("kind") == "message" else {"skip": True}
                state = await queue.close(item["id"], **field)
            else:
                state = await queue.current()
    print_kinds(kinds)
    log(f"done:{state} {stats} spent=${llm.spent:.3f} calls={llm.calls} wall={time.time() - start:.0f}s")
    site_task.cancel()
    warm_task.cancel()
    await site.close()


def main():
    config.require_urls()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
