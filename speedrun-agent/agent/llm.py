"""OpenAI-compatible chat client for the judge's OpenRouter proxy."""
import asyncio
import re
import time

from . import config
from .http import request


class BudgetExhausted(Exception):
    pass


class LLM:
    def __init__(self):
        self.model = config.MODEL
        self.spent = 0.0
        self.calls = 0
        self.budget_gone = False
        self.ready = asyncio.Event()   # set once the model is chosen; calls made before that wait
        self.limit = 32                # requests in flight allowed by the proxy (lowered on 429)
        self.inflight = 0
        self.cv = asyncio.Condition()
        self.fallback = ""             # fast model (see config.FALLBACK_PREFS)
        self.use_fallback = False      # set once the strong model's budget share is spent or it keeps failing

    def _headers(self):
        return {"Authorization": f"Bearer {config.LLM_API_KEY}"}

    async def pick_model(self):
        try:
            await self._pick_model()
        finally:
            self.ready.set()

    async def _pick_model(self):
        if self.model:
            print(f"[llm] model {self.model} (from MODEL)", flush=True)
            return
        # Listing runs in the background; a slow listing must not hold the first tasks (they wait on self.ready
        # inside their own caps), so give up on it quickly.
        status, data = await request("GET", f"{config.LLM_BASE_URL}/models", headers=self._headers(), timeout=5)
        ids = set()
        if status == 200:
            ids = {m.get("id") for m in data.get("data", [])}
        for m in config.MODEL_PREFS:
            if m in ids:
                self.model = m
                break
        if not self.model and ids:
            # None of our picks is listed: take a fast, cheap-looking model that supports tools.
            for pat in ("gemini-2.5-flash", "flash", "gpt-4.1-mini", "mini", "haiku"):
                cands = sorted(i for i in ids if i and pat in i and "image" not in i and "lite" not in i
                               and not i.endswith(":free") and "audio" not in i)
                if cands:
                    self.model = cands[-1]
                    break
        if not self.model:
            self.model = config.MODEL_PREFS[0]
        self.fallback = next((m for m in config.FALLBACK_PREFS if m in ids and m != self.model), "")
        print(f"[llm] model {self.model} ({len(ids)} models listed), fallback {self.fallback or '(none)'}", flush=True)

    async def _slot(self):
        """Wait for a free request slot. The proxy allows fewer requests in flight as the budget shrinks
        (practice, $1 left: 1 at a time) and answers 429 concurrency_limit beyond that."""
        async with self.cv:
            await self.cv.wait_for(lambda: self.inflight < self.limit)
            self.inflight += 1

    async def _free(self):
        async with self.cv:
            self.inflight -= 1
            self.cv.notify_all()

    async def chat(self, messages, tools=None, max_tokens=4000, timeout=60, reasoning=None, model=None):
        """Returns (message, usage, seconds). message["_finish"] is the finish reason ("length" = cut off).
        Thinking models spend reasoning tokens out of max_tokens, so caps must stay generous."""
        await self.ready.wait()
        if self.budget_gone:
            raise BudgetExhausted()
        if self.fallback and not self.use_fallback and self.spent >= config.BUDGET * config.STRONG_BUDGET_SHARE:
            self.use_fallback = True
            print(f"[llm] ${self.spent:.2f} spent: switching from {self.model} to {self.fallback}", flush=True)
        model = model or (self.fallback if self.use_fallback and self.fallback else self.model)
        original = messages
        if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
            # "<<DYNAMIC>>" splits the system prompt: before it, instructions and the site guide, the same for every
            # task; after it, the date and the task's rule messages. Claude caches the fixed part across tasks and
            # the whole prompt across the turns of one task.
            fixed, _, dynamic = messages[0]["content"].partition("<<DYNAMIC>>")
            if model.startswith("anthropic/"):
                parts = [{"type": "text", "text": fixed, "cache_control": {"type": "ephemeral"}}]
                if dynamic.strip():
                    parts.append({"type": "text", "text": dynamic, "cache_control": {"type": "ephemeral"}})
                messages = [{"role": "system", "content": parts}] + messages[1:]
            else:
                messages = [{"role": "system", "content": fixed + dynamic}] + messages[1:]
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        effort = reasoning or config.REASONING_EFFORT
        if effort:
            body["reasoning"] = {"effort": effort}
            if model.startswith("anthropic/"):
                body.pop("temperature")    # Claude refuses a temperature other than 1 while thinking
        errors = 0
        while errors < 4:
            t0 = time.time()
            await self._slot()
            try:
                status, data = await request("POST", f"{config.LLM_BASE_URL}/chat/completions", body,
                                             headers=self._headers(), timeout=timeout)
            finally:
                await self._free()
            self.calls += 1
            if status == 200 and data.get("choices"):
                usage = data.get("usage") or {}
                self.spent += float(usage.get("cost") or 0)
                msg = data["choices"][0].get("message") or {}
                msg["_finish"] = data["choices"][0].get("finish_reason") or ""
                return msg, usage, time.time() - t0
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            code = err.get("code") or err.get("type")
            if status == 402 or code == "cost_cap_reached":
                self.budget_gone = True
                raise BudgetExhausted()
            if status == 429 and code == "concurrency_limit":
                m = re.search(r"may have (\d+) request", str(err.get("message")))
                self.limit = max(1, int(m.group(1))) if m else max(1, self.limit - 1)
                print(f"[llm] concurrency limit now {self.limit}", flush=True)
                await asyncio.sleep(0.2)    # not an error: wait for a slot (the caller's timeout bounds this)
                continue
            errors += 1
            print(f"[llm] error {status} {str(data)[:200]}", flush=True)
            low = str(data).lower()
            if status == 400 and "temperature" in body and "temperature" in low:
                body.pop("temperature")
                continue
            if status == 400 and "cache_control" in low and isinstance(body["messages"][0].get("content"), list):
                body["messages"] = [{"role": "system", "content": "".join(p["text"] for p in body["messages"][0]["content"])}] \
                    + body["messages"][1:]
                continue
            if status == 400 and "reasoning" in body:
                body.pop("reasoning")      # some models refuse the field
                continue
            await asyncio.sleep(0.5 * errors)
        if model != self.fallback and self.fallback and status not in (0, 408, 504):
            # this one call keeps failing on the strong model (not a timeout): try it once on the fast one.
            # Only this call: one bad request must not move the rest of the shift off the strong model.
            print(f"[llm] {model} failed ({status}): this call goes to {self.fallback}", flush=True)
            return await self.chat(original, tools, max_tokens, timeout, reasoning, model=self.fallback)
        raise RuntimeError(f"LLM failed: {status}")
