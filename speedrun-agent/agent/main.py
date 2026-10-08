"""Speedrun agent: works the queue until the shift ends."""
import asyncio
import os
import time

from . import config
from .llm import LLM, BudgetExhausted
from .memory import Memory
from .queue import Queue
from .site import Site
from .solver import Solver


def log(*a):
    print("[agent]", *a, flush=True)


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

    stats = {"answered": 0, "skipped": 0, "messages": 0}
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
            deadline = min(time.time() + config.task_cap(secs_left, tasks_left), shift_end)
            # When time is short, kinds of task that the model loop never answered get only the deterministic paths.
            allow_loop = not (secs_left / tasks_left < config.TIGHT_FAIR_SECONDS
                              and memory.hopeless(item["text"], config.HOPELESS_AFTER))
            if allow_loop and memory.answered_like(item["text"]) \
                    and secs_left / tasks_left > config.KNOWN_KIND_MIN_FAIR:
                deadline = max(deadline, min(time.time() + config.KNOWN_KIND_SECONDS, shift_end))
            if not allow_loop and memory.explore_turn(item["text"], config.EXPLORE_EVERY) \
                    and secs_left - config.EXPLORE_SECONDS > tasks_left * config.SKIP_SECONDS:
                allow_loop = True
                deadline = min(time.time() + config.EXPLORE_SECONDS, shift_end)
                log(f"{item['id']} exploring a hopeless kind for up to {config.EXPLORE_SECONDS:.0f}s")
            # Model listing is not awaited (LLM.chat waits on llm.ready inside the task's own caps), and sign-in is
            # waited for only until this task's deadline: a slow startup can cost the first tasks, never the shift.
            try:
                await asyncio.wait_for(site.ready.wait(), max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                kind, value, info = "skip", None, {"why": "site not ready"}
            else:
                try:   # with the model budget gone, the deterministic paths that need no model still answer
                    kind, value, info = await solver.solve(item["text"], deadline, allow_loop=allow_loop)
                except BudgetExhausted:
                    kind, value, info = "skip", None, {"why": "budget"}
                except Exception as e:
                    kind, value, info = "skip", None, {"why": f"error {e!r}"[:150]}
            if time.time() > shift_end + config.SAFETY_SECONDS:
                log("shift is over")
                break
            if kind == "answer":
                stats["answered"] += 1
                memory.add(item["text"], info.get("route", []), value)
                state = await queue.close(item["id"], answer=value)
            else:
                stats["skipped"] += 1
                state = await queue.close(item["id"], skip=True)
            log(f"{item['id']} {kind.upper()} {value!r} in {time.time() - t0:.1f}s | {info.get('why', '')} "
                f"conf={info.get('conf', '')} pages={len(info.get('route', []))} | task: {item['text']!r}")
            log(f"   left {state.get('seconds_left', 0):.0f}s  spent ${llm.spent:.3f}  {stats}")
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
    log(f"done: {state} {stats} spent=${llm.spent:.3f} calls={llm.calls} wall={time.time() - start:.0f}s")
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
