"""Headless-browser access to Wrenfield.

What the practice clinic showed (Wrenfield 4.2.0):
- Every page is a shell (header with the location switcher, sidebar) plus a nested iframe /v/<random>/ with the
  content. Some pages nest one more iframe (the document viewer). A script checks the real layout before the data
  appears, so we use a real browser and read every frame.
- The working location, the rows-per-page setting and the page number of a list are server-side session state,
  changed with POSTs (and a page reload). URL parameters do not page a list. So we switch to "All locations"
  with real clicks and check that it took, and we page through lists by clicking.
- Documents open in a viewer with a "Download" link to a text PDF; we read its text with pypdf.
- Provider pages (/schedule/providers/<id>) show "<Name>" then "<Specialty> · <weekday, date>". The provider
  select has Active and Inactive groups, so providers who left are listed too.
"""
import asyncio
import io
import re
import time
from urllib.parse import urlsplit

from . import config

# Runs in one frame. Returns its text with every table rendered as pipe rows that carry their links, so the model
# can tell which "Jones, Carol" link belongs to which date of birth.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const body = document.body;
  if (!body) return null;
  let text = body.innerText || '';
  const tables = [];
  for (const t of document.querySelectorAll('table')) {
    const headers = [...t.querySelectorAll('thead th, thead td')].map(c => clean(c.innerText));
    const rows = [];
    for (const tr of t.querySelectorAll('tbody tr, tr')) {
      if (tr.closest('thead')) continue;
      const cells = [...tr.children].filter(c => c.tagName === 'TD' || c.tagName === 'TH').map(c => clean(c.innerText));
      if (!cells.length || cells.every(c => !c)) continue;
      if (!headers.length && tr.querySelector('th') && !tr.querySelector('td')) { headers.push(...cells); continue; }
      const links = [...tr.querySelectorAll('a[href]')].map(a => [clean(a.innerText).slice(0, 70), a.getAttribute('href')]);
      rows.push({cells, links});
    }
    if (!rows.length) continue;
    const lines = [];
    if (headers.length) lines.push('## ' + headers.join(' | '));
    for (const r of rows) {
      let ln = r.cells.join(' | ');
      const hrefs = [...new Set(r.links.map(l => l[1]))];
      if (hrefs.length) ln += '  -> ' + hrefs.join(' , ');
      lines.push(ln);
    }
    const rendered = '[table]\n' + lines.join('\n') + '\n[/table]';
    const raw = t.innerText || '';
    if (raw && text.includes(raw)) text = text.replace(raw, rendered);
    else text += '\n' + rendered;
    tables.push({headers, rows});
  }
  const links = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript')) continue;
    const label = clean(a.innerText || a.textContent).slice(0, 70);
    const key = href + '|' + label;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push([label, href]);
  }
  const forms = [];
  for (const f of document.querySelectorAll('form')) {
    const fields = [];
    for (const e of f.elements) {
      if (!e.name || e.type === 'submit' || (e.type === 'hidden' && !e.value)) continue;
      let d = e.name + '(' + (e.tagName === 'SELECT' ? 'select' : e.type || e.tagName.toLowerCase()) + ')';
      if (e.tagName === 'SELECT') {
        d += ' options: ' + [...e.options].slice(0, 80).map(o => (o.value === '' ? "''" : o.value) + '=' + clean(o.text)).join('; ');
      }
      if (e.value && e.tagName !== 'SELECT') d += ' current=' + String(e.value).slice(0, 40);
      fields.push(d);
    }
    if (fields.length) forms.push(((f.getAttribute('method') || 'get').toUpperCase()) + ' ' + (f.getAttribute('action') || '(this page)') + ' :: ' + fields.join(' | '));
  }
  const selects = [];
  for (const s of document.querySelectorAll('select')) {
    const opts = [...s.options].map(o => ({value: o.value, text: clean(o.text),
      group: o.parentElement && o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : ''}));
    selects.push({name: s.name || '', options: opts});
  }
  const media = [];
  for (const e of document.querySelectorAll('img, embed, object, canvas')) {
    const r = e.getBoundingClientRect();
    if (r.width < 120 || r.height < 120) continue;
    media.push(e.tagName.toLowerCase() + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) +
      (e.getAttribute('alt') ? ' alt="' + clean(e.getAttribute('alt')).slice(0, 80) + '"' : ''));
  }
  const files = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    const label = clean(a.innerText);
    if (a.hasAttribute('download') || /\.pdf($|\?)/i.test(href) || /^(Download|Open in new tab)$/i.test(label)) {
      files.push({href: a.href || href, name: a.getAttribute('download') || label});  // a.href: absolute, resolved in this frame
    }
  }
  return {title: document.title, text, links, forms, selects, media, files, tables};
}
"""

PROBE_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  const ifr = [...document.querySelectorAll('iframe')].filter(f => { const r = f.getBoundingClientRect(); return r.width > 50 && r.height > 50; }).length;
  return [t.length, /Loading Wrenfield|is not responding/.test(t.slice(0, 400)) ? 1 : 0, ifr];
}
"""

# Which option of the location switcher is selected (works whether the menu is open or closed).
LOCATION_STATE_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = {selected: null, options: [], trigger: null};
  for (const b of document.querySelectorAll('button, [role=option], [role=menuitemradio], [role=menuitem]')) {
    const t = clean(b.innerText || b.textContent);
    if (!t || t.length > 80) continue;
    const on = ['aria-pressed', 'aria-selected', 'aria-checked'].some(a => b.getAttribute(a) === 'true');
    const inner = b.querySelector('span span') ? [...b.querySelector('span span').parentElement.children].map(s => clean(s.innerText)) : null;
    if (/^All locations$/.test(t) || (inner && inner.length === 2 && /,\s*[A-Z]{2}$/.test(inner[1]))) {
      const name = inner && inner.length === 2 ? inner[0] : t;
      out.options.push({name, place: inner && inner.length === 2 ? inner[1] : ''});
      if (on) out.selected = name;
    }
  }
  // other versions: a native <select> switcher (not a GET list filter), or an item marked current/selected/active
  for (const s of document.querySelectorAll('select')) {
    const f = s.closest('form');
    if (f && (f.getAttribute('method') || 'get').toLowerCase() === 'get') continue;
    const all = [...s.options].find(o => /^\s*All locations\s*$/.test(o.text));
    if (!all) continue;
    for (const o of s.options) out.options.push({name: clean(o.text), place: ''});
    if (s.options[s.selectedIndex] === all) out.selected = 'All locations';
    else if (!out.selected && s.selectedIndex >= 0) out.selected = clean(s.options[s.selectedIndex].text);
    out.native = true;
  }
  for (const e of document.querySelectorAll('[aria-current], [data-selected=true], [data-state=checked], .active, .selected, .current')) {
    const t = clean(e.innerText || e.textContent);
    if (/^All locations$/.test(t) && !out.selected) out.selected = 'All locations';
  }
  const body = document.body ? document.body.innerText : '';
  const m = body.match(/Location\s*\n\s*([^\n]+)/);
  if (m) out.trigger = clean(m[1]);
  return out;
}
"""

# What the page offers for switching location, printed when the switch cannot be confirmed (other clinic versions).
LOCATION_DEBUG_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const e of document.querySelectorAll('button, a, select, [role], li, label')) {
    const t = clean(e.innerText || e.textContent);
    if (!/locat/i.test(t + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('name') || ''))) continue;
    if (t.length > 120) continue;
    out.push(e.tagName.toLowerCase() + (e.getAttribute('role') ? '[' + e.getAttribute('role') + ']' : '') +
             (e.getAttribute('name') ? '{' + e.getAttribute('name') + '}' : '') + ': ' + t.slice(0, 100));
    if (out.length >= 15) break;
  }
  return out;
}
"""

SHOWING = re.compile(r"Showing\s+([\d,]+)\s+(?:to|-|–)\s+([\d,]+)\s+of\s+([\d,]+)", re.I)


def _int(s):
    return int(str(s).replace(",", ""))


def same_origin(url):
    """Rule 1: the browser may reach only SITE_URL (same scheme, host and port); data:, blob: and about: never leave the box."""
    def origin(s):   # Chromium reports hosts in lower case and drops a default port: compare normalised parts
        u = urlsplit(s)
        return u.scheme, u.hostname, u.port or {"http": 80, "https": 443}.get(u.scheme)
    try:
        return urlsplit(url).scheme in ("data", "blob", "about") or origin(url) == origin(config.SITE_URL)
    except ValueError:   # a port that is not a number in 0-65535
        return False


class Site:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.ctx = None
        self.pages = asyncio.Queue()
        self.cache = {}           # path -> rendered dict
        self.cache_t = {}         # path -> when it was read: the clinic's data changes during the shift
        self.nav_links = set()    # hrefs that appear on every page (sidebar): hidden from the model
        self.ready = asyncio.Event()
        self.error = None
        self.inflight = {}        # page -> number of requests in flight
        self.list_lock = asyncio.Lock()   # paging a list changes session state: one at a time
        self.location_ok = False
        self.locations = {}       # location name -> "City, ST"
        self.today = ""           # clinic date from the dashboard, e.g. "Oct 14, 2026"
        self._providers = None    # list of {name, value, group}
        self._search_params = None
        self._prov_lock = asyncio.Lock()
        self.specialty_cache = {}
        self.dumped = {}          # page pattern -> times printed (see _dump)
        self.digit_map = {}       # scrambled page-text digit -> digit on screen (learned per session)
        self.digit_map_ok = True
        self.digit_map_hits = 0   # screenshots that agreed with the map; after 3 we trust it (re-checked every 8th)
        self.phone_reads = 0
        self.login_lock = asyncio.Lock()
        self.last_login = 0.0
        self.relogins = 0
        self.blocked = set()      # hosts the browser was stopped from reaching, printed once each; kept across retries
        # startup state for the DIAG-01 probe
        self.signed_in = False
        self.startup = {}
        self.search_hits = 0
        self.search_misses = 0
        self.dob_searches = set()

    # ---------- startup ----------
    async def start(self):
        """Browser, sign-in, All locations, dashboard. A failure is retried: without the site every task is a skip."""
        for attempt in range(3):
            self.error = None
            await self._start_once()
            if not self.error:
                break
            print(f"[site] startup attempt {attempt + 1} failed; retrying", flush=True)
            await self.close()
            self.pages = asyncio.Queue()
            self.inflight = {}
            await asyncio.sleep(2)
        # a snapshot, because _relogin resets location_ok mid-shift
        self.startup = {"signed_in": self.signed_in and not self.error, "location_ok": self.location_ok and not self.error}
        self.ready.set()

    async def _start_once(self):
        try:
            # Imported here: a missing or broken Playwright is then a caught startup failure like any other, and the
            # logic tests can import this module without Playwright (same lazy pattern as pypdf in pdf_text).
            from playwright.async_api import async_playwright
            t0 = time.time()
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            # service_workers="block": context routes do not see requests a service worker handles.
            self.ctx = await self.browser.new_context(viewport={"width": 1400, "height": 1000}, service_workers="block")
            self.ctx.set_default_timeout(config.NAV_TIMEOUT * 1000)

            async def _guard(route):
                if same_origin(route.request.url):
                    return await route.continue_()
                host = urlsplit(route.request.url).netloc
                if host not in self.blocked:
                    self.blocked.add(host)
                    print(f"[site] blocked a request to {host} (Rule 1)", flush=True)
                await route.abort()
            # Rule 1 guard on the context, before the first tab: it covers every pooled tab, popup and frame.
            # Do NOT block fonts or images: the anti-bot check hashes the rendered layout, which depends on fonts
            # (everything on the SITE_URL origin passes).
            # limit: a redirect from SITE_URL to another host is followed without calling the handler again (Playwright routes the original request only); the judge network refuses other hosts
            await self.ctx.route("**/*", _guard)
            first = await self._new_page()
            await self._login(first)
            self.last_login = time.time()
            try:
                if await self._wait_content(first, seconds=4):
                    await self._pass_notice(first, await self._extract(first))
            except Exception as e:
                print(f"[site] notice check failed: {e!r}"[:200], flush=True)
            await self._all_locations(first)
            await self._read_dashboard(first)
            self.pages.put_nowait(first)
            extra = await asyncio.gather(*[self._new_page() for _ in range(max(0, config.PAGE_POOL - 1))])
            for p in extra:
                self.pages.put_nowait(p)
            # navigation learned from the dashboard: what Phase 2 (ROB-02) needs to know about an unseen clinic
            nav = sorted({h.split("?")[0] for _, h in self.cache.get("/", {}).get("links", []) if h.startswith("/")})
            print(f"[site] ready in {time.time() - t0:.1f}s  sign-in={self.signed_in} location_ok={self.location_ok} "
                  f"today={self.today!r} nav={len(nav)} {nav[:25]}", flush=True)
        except Exception as e:  # keep the agent alive; tasks will be skipped
            self.error = repr(e)
            print(f"[site] startup failed: {e!r}", flush=True)

    async def _new_page(self):
        page = await self.ctx.new_page()
        self.inflight[page] = 0

        def up(_req, p=page):
            self.inflight[p] = self.inflight.get(p, 0) + 1

        def down(_req, p=page):
            self.inflight[p] = max(0, self.inflight.get(p, 0) - 1)

        page.on("request", up)
        page.on("requestfinished", down)
        page.on("requestfailed", down)
        return page

    async def _find_frame(self, page, selector, seconds=8):
        end = time.time() + seconds
        while time.time() < end:
            for fr in page.frames:
                try:
                    el = await fr.query_selector(selector)
                    if el:
                        return fr, el
                except Exception:
                    pass
            await asyncio.sleep(0.1)
        return None, None

    async def _login(self, page):
        """Sign in. Clinic versions differ: the form may live at /login, or / may redirect to it under another name."""
        fr = pw_el = None
        for path, secs in (("/login", 6), ("/", 4)):
            try:
                await page.goto(config.SITE_URL + path, wait_until="commit")
            except Exception as e:
                print(f"[site] {path} did not open: {e!r}"[:200], flush=True)
                continue
            fr, pw_el = await self._find_frame(page, "input[type=password]", seconds=secs)
            if pw_el:
                break
        if not pw_el:
            print(f"[site] no login form (already signed in or open site), at {page.url}", flush=True)
            # an open site such as practice counts as signed in, a page that never loaded does not
            self.signed_in = page.url.startswith(config.SITE_URL)
            return
        user_el = None
        for sel in ["input[type=email]", "input[autocomplete=username]", "input[name*=user i]", "input[id*=user i]",
                    "input[name*=login i]", "input[name*=email i]", "input[name*=name i]", "input[type=text]",
                    "input:not([type])"]:
            user_el = await fr.query_selector(sel)
            if user_el:
                break
        if user_el:
            await user_el.fill(config.SITE_USERNAME)
        await pw_el.fill(config.SITE_PASSWORD)
        await pw_el.press("Enter")
        for fallback in range(3):
            end = time.time() + 2.5
            while time.time() < end and "/login" in page.url:
                await asyncio.sleep(0.1)
            if "/login" not in page.url:
                break
            try:
                if fallback == 0:
                    btn = await fr.query_selector("button[type=submit], input[type=submit], form button")
                    if btn:
                        await btn.click()
                        continue
                await pw_el.evaluate("e => { const f = e.form; if (f) { f.requestSubmit ? f.requestSubmit() : f.submit() } }")
            except Exception:
                pass
        still = await self._find_frame(page, "input[type=password]", seconds=0.5)
        self.signed_in = not still[1] and "/login" not in page.url
        print(f"[site] signed in, now at {page.url}" + (" (WARNING: a password field is still shown)" if still[1] else ""),
              flush=True)

    async def _pass_notice(self, page, data):
        """A notice or consent page in front of the app ('Acknowledge', 'I agree'): click through it once per page.
        True when one was clicked (read the page again)."""
        if len(data.get("links", [])) > 8 or len(data["text"]) > 3000:
            return False   # the app itself: many links
        if not NOTICE.search(data["text"][:2000]) or re.search(r"restricted|give a reason", data["text"], re.I):
            return False   # a restricted chart is not a notice: the agent never asks for access
        for kind in ("button", "link"):
            if await self._click_in_frames(page, kind, NOTICE_BTN):
                print(f"[site] clicked through a notice page at {page.url}", flush=True)
                await asyncio.sleep(0.5)
                return True
        return False

    @staticmethod
    def _is_login_page(data, url):
        return any("(password)" in f for f in data.get("forms", [])) or bool(re.search(r"/(login|signin|sign-in)\b", url or ""))

    async def _relogin(self, page):
        """The session ended (or never started): sign in again, at most a few times a shift."""
        async with self.login_lock:
            if time.time() - self.last_login < 5:
                return True       # another tab just did it
            if self.relogins >= 4:
                return False
            self.relogins += 1
            print(f"[site] signed out: signing in again ({self.relogins})", flush=True)
            try:
                self.location_ok = False   # a new session starts at the default (one) location
                await self._login(page)
                if not self.location_ok:
                    await self._all_locations(page)
            except Exception as e:
                print(f"[site] sign-in again failed: {e!r}"[:200], flush=True)
                return False
            self.last_login = time.time()
            self.cache.clear()
            return True

    async def _location_state(self, page):
        best = {"selected": None, "options": [], "trigger": None}
        for fr in page.frames:
            try:
                st = await fr.evaluate(LOCATION_STATE_JS)
            except Exception:
                continue
            if st.get("selected") and not best["selected"]:
                best["selected"] = st["selected"]
            if st.get("trigger") and not best["trigger"]:
                best["trigger"] = st["trigger"]
            best["options"] += st.get("options", [])
        return best

    def _location_is_all(self, st):
        return st.get("selected") == "All locations" or (st.get("trigger") or "").startswith("All locations")

    async def _all_locations(self, page):
        """Patient search and charts are scoped to the working location. Switch to All locations with real
        (trusted) clicks, then check the switcher really says so. The switch is a POST plus a reload."""
        if "/login" in page.url or page.url == "about:blank":
            await page.goto(config.SITE_URL + "/", wait_until="commit")
        await self._wait_content(page)
        for attempt in range(4):
            st = await self._location_state(page)
            self._learn_locations(st)
            if self._location_is_all(st):
                self.location_ok = True
                print(f"[site] location is All locations (attempt {attempt})", flush=True)
                return
            clicked = await self._click_all_locations(page, use_js=(attempt == 3))
            if clicked:
                try:
                    await page.wait_for_load_state("load", timeout=4000)
                except Exception:
                    pass
                await asyncio.sleep(0.4)
                await self._wait_content(page)
        st = await self._location_state(page)
        self.location_ok = self._location_is_all(st)
        print(f"[site] WARNING location switch not confirmed: {st.get('selected') or st.get('trigger')!r}"
              if not self.location_ok else "[site] location is All locations", flush=True)
        if not self.location_ok:
            for fr in page.frames:
                try:
                    found = await fr.evaluate(LOCATION_DEBUG_JS)
                except Exception:
                    continue
                if found:
                    print(f"[site] location controls in {fr.url[:80]}: {found}"[:1500], flush=True)

    def _learn_locations(self, st):
        for o in st.get("options", []):
            if o.get("place"):
                self.locations[o["name"]] = o["place"]

    async def _click_all_locations(self, page, use_js=False):
        target_re = re.compile(r"^\s*All locations\s*$")
        # a native <select> switcher (not a GET list filter): choose the option, then submit its form if it has one
        for fr in page.frames:
            try:
                sels = fr.locator("select").filter(has=fr.locator("option", has_text=target_re))
                for i in range(await sels.count()):
                    s = sels.nth(i)
                    method = await s.evaluate("s => s.form ? (s.form.getAttribute('method') || 'get').toLowerCase() : 'none'")
                    if method == "get":
                        continue
                    await s.select_option(label="All locations")
                    await s.evaluate("s => { s.dispatchEvent(new Event('change', {bubbles: true})); "
                                     "if (s.form) { s.form.requestSubmit ? s.form.requestSubmit() : s.form.submit() } }")
                    return True
            except Exception as e:
                print(f"[site] location select failed: {e!r}"[:200], flush=True)
        for fr in page.frames:
            try:
                opt = fr.locator("button, [role=option], [role=menuitem], [role=menuitemradio]").filter(has_text=target_re)
                if await opt.count() == 0:
                    continue
                if use_js:
                    await opt.first.evaluate("e => e.click()")
                    return True
                if not await opt.first.is_visible():
                    # open the switcher: the control that shows "Location" and the current name
                    trig = fr.locator("button, [role=button], [aria-haspopup]").filter(has_text=re.compile(r"Location"))
                    if await trig.count():
                        await trig.first.click(timeout=3000)
                    else:
                        await fr.get_by_text("Location", exact=True).first.click(timeout=3000)
                    await asyncio.sleep(0.25)
                await opt.first.click(timeout=3000)
                return True
            except Exception as e:
                print(f"[site] location click failed: {e!r}"[:200], flush=True)
        # any other control: open whatever shows the word Location, then click an exact "All locations" item
        for fr in page.frames:
            try:
                item = fr.get_by_text(target_re)
                if await item.count() == 0:
                    continue
                if not await item.first.is_visible():
                    trig = fr.get_by_text(re.compile(r"locations?", re.I))
                    for j in range(min(await trig.count(), 4)):
                        if await trig.nth(j).is_visible():
                            await trig.nth(j).click(timeout=2000)
                            await asyncio.sleep(0.25)
                            if await item.first.is_visible():
                                break
                await item.first.click(timeout=3000)
                return True
            except Exception as e:
                print(f"[site] location text click failed: {e!r}"[:200], flush=True)
        return False

    async def _read_dashboard(self, page):
        try:
            await page.goto(config.SITE_URL + "/", wait_until="commit")
            await self._wait_content(page)
            data = await self._extract(page)
            m = re.search(r"\b([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})\b", data["text"])
            if m:
                self.today = m.group(1)
            data["path"] = "/"
            self.cache["/"] = data
        except Exception as e:
            print(f"[site] dashboard read failed: {e!r}", flush=True)

    # ---------- reading pages ----------
    async def _wait_content(self, page, seconds=None):
        """Wait until the page's content frames have rendered: every visible iframe has text, no request has been
        in flight for a moment, and the text has stopped changing. Returns True when content is there."""
        t0 = time.time()
        end = t0 + (seconds or config.NAV_TIMEOUT)
        last_len, stable_since, idle_since = -1, None, None
        while time.time() < end:
            total, loading, want_frames, have_frames = 0, False, 0, 0
            for fr in page.frames:
                try:
                    n, ld, ifr = await fr.evaluate(PROBE_JS)
                except Exception:
                    continue
                want_frames += ifr
                if fr != page.main_frame:
                    if n > 20:
                        have_frames += 1
                    total += n
                elif len(page.frames) == 1:
                    total += n
                loading = loading or bool(ld)
            now = time.time()
            if self.inflight.get(page, 0) == 0:
                idle_since = idle_since or now
            else:
                idle_since = None
            if total != last_len:
                last_len, stable_since = total, now
            frames_ok = have_frames >= want_frames or now - t0 > 2.5
            if total > 20 and not loading and frames_ok and stable_since:
                quiet = now - stable_since
                if (idle_since and now - idle_since >= 0.15 and quiet >= 0.12) or quiet >= 0.8:
                    return True
            await asyncio.sleep(0.04)
        return last_len > 20

    async def _extract(self, page):
        """Text, tables, links and forms of every content frame (parents before children)."""
        frames = [f for f in page.frames if f != page.main_frame] or [page.main_frame]
        parts = []
        for fr in frames:
            try:
                d = await fr.evaluate(EXTRACT_JS)
            except Exception:
                continue
            if d and len(d.get("text", "").strip()) > 0:
                d["frame"] = fr
                parts.append(d)
        if not parts:
            return {"text": "", "links": [], "forms": [], "selects": [], "media": [], "files": [], "tables": [], "title": ""}
        out = {"title": parts[0].get("title", ""), "text": "", "links": [], "forms": [], "selects": [], "media": [],
               "files": [], "tables": []}
        seen_links = set()
        for i, d in enumerate(parts):
            out["text"] += ("\n\n[nested frame]\n" if i else "") + d["text"]
            for l in d["links"]:
                if tuple(l) not in seen_links:
                    seen_links.add(tuple(l))
                    out["links"].append(l)
            for k in ("forms", "selects", "media", "files", "tables"):
                out[k] += d.get(k, [])
        return out

    def _normalize(self, path):
        path = (path or "/").strip()
        if path.startswith(config.SITE_URL):
            path = path[len(config.SITE_URL):] or "/"
        if path.startswith("http"):
            path = "/" + path.split("/", 3)[-1]
        if not path.startswith("/"):
            path = "/" + path
        return path

    async def read(self, path, all_pages=True, fresh=False):
        path = self._normalize(path)
        if not fresh and path in self.cache and time.time() - self.cache_t.get(path, 0) < config.CACHE_SECONDS:
            return self.cache[path]
        await self.ready.wait()
        if self.error and self.pages.empty():
            return {"path": path, "error": "browser not available: " + self.error}
        page = await self.pages.get()
        try:
            for attempt in range(4):
                try:
                    if self.login_lock.locked():   # a re-login is running: wait, so nothing is read from the old session
                        async with self.login_lock:
                            pass
                    await page.goto(config.SITE_URL + path, wait_until="commit")
                    # A sparse sign-in page never counts as content, so waiting first spent 4 x NAV_TIMEOUT per read
                    # before the sign-in check below: signed-out reads never returned within a task (Plan 01-03).
                    if not self._is_login_page({}, page.url) and not await self._wait_content(page):
                        continue
                    data = await self._extract(page)
                    if "is not responding" in data["text"][:300]:
                        continue
                    if TRANSIENT.search(data.get("title") or "") or \
                            (len(data["text"]) < 500 and TRANSIENT.search(data["text"])):
                        # never cache or hand back a 503 page: it reads as an empty chart, list or schedule
                        print(f"[site] {path}: {(data.get('title') or data['text'][:60])!r}, retrying", flush=True)
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    if await self._pass_notice(page, data):
                        continue
                    if self._is_login_page(data, page.url) and not re.search(r"log ?in|sign ?in", path, re.I):
                        if await self._relogin(page):
                            continue
                        return {"path": path, "error": "signed out and could not sign in again"}
                    m = SHOWING.search(data["text"])
                    if all_pages and m and _int(m.group(2)) < _int(m.group(3)):
                        data = await self._all_rows(page, path, data)
                    data["path"] = path
                    data["final_url"] = page.url
                    self.cache[path] = data
                    self.cache_t[path] = time.time()
                    self._dump(data)
                    return data
                except Exception as e:
                    print(f"[site] read {path} failed: {e!r}"[:200], flush=True)
            return {"path": path, "error": "page did not load"}
        finally:
            self.pages.put_nowait(page)

    # ---------- lists that span several pages ----------
    async def _click_in_frames(self, page, kind, name_re):
        for fr in page.frames:
            try:
                loc = fr.get_by_role(kind, name=name_re)
                n = await loc.count()
                for i in range(n):
                    el = loc.nth(i)
                    if await el.is_visible() and await el.is_enabled():
                        await el.click(timeout=3000)
                        return True
            except Exception:
                continue
        return False

    async def _after_action(self, page, before_text):
        """Wait for the list to change after a click or select (a POST, maybe a reload)."""
        end = time.time() + 5
        await asyncio.sleep(0.15)
        while time.time() < end:
            await self._wait_content(page, seconds=max(0.5, end - time.time()))
            data = await self._extract(page)
            m1, m0 = SHOWING.search(data["text"]), SHOWING.search(before_text)
            if m1 and (not m0 or m1.groups() != m0.groups()):
                return data
            await asyncio.sleep(0.15)
        return await self._extract(page)

    async def _all_rows(self, page, path, data):
        """Collect every row of a list that is split into pages: largest page size, then Next until done."""
        async with self.list_lock:
            m = SHOWING.search(data["text"])
            first_text = data["text"]
            # 1) largest rows-per-page option
            for fr in page.frames:
                try:
                    sel = fr.locator("select").filter(has=fr.locator("option", has_text=re.compile("per page", re.I)))
                    if await sel.count():
                        labels = [t.strip() for t in await sel.first.locator("option").all_inner_texts()]
                        nums = [(int(re.search(r"\d+", t).group()), t) for t in labels if re.search(r"\d+", t)]
                        if nums:
                            best = max(nums)[1]
                            cur = await sel.first.evaluate("s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text.trim() : ''")
                            if cur != best:
                                await sel.first.select_option(label=best)
                                data = await self._after_action(page, data["text"])
                        break
                except Exception as e:
                    print(f"[site] page size change failed on {path}: {e!r}"[:200], flush=True)
            # 2) back to the first page if the session left us further on
            m = SHOWING.search(data["text"])
            if m and _int(m.group(1)) > 1:
                if await self._click_in_frames(page, "button", re.compile(r"^\s*1\s*$")) or \
                        await self._click_in_frames(page, "link", re.compile(r"^\s*1\s*$")):
                    data = await self._after_action(page, data["text"])
            pages = [data]
            # 3) Next until the last row
            for _ in range(80):
                m = SHOWING.search(pages[-1]["text"])
                if not m or _int(m.group(2)) >= _int(m.group(3)):
                    break
                ok = await self._click_in_frames(page, "button", re.compile(r"^\s*Next\s*$", re.I)) or \
                    await self._click_in_frames(page, "link", re.compile(r"^\s*Next\s*$", re.I))
                if not ok:
                    break
                nxt = await self._after_action(page, pages[-1]["text"])
                m2 = SHOWING.search(nxt["text"])
                if not m2 or m2.groups() == m.groups():
                    break
                pages.append(nxt)
            merged = pages[0]
            for p in pages[1:]:
                rows_text = []
                for t in p["tables"]:
                    for r in t["rows"]:
                        ln = " | ".join(r["cells"])
                        hrefs = list(dict.fromkeys(h for _, h in r["links"]))
                        if hrefs:
                            ln += "  -> " + " , ".join(hrefs)
                        rows_text.append(ln)
                merged["text"] += "\n[more rows, next page]\n" + "\n".join(rows_text)
                seen = {tuple(l) for l in merged["links"]}
                merged["links"] += [l for l in p["links"] if tuple(l) not in seen]
                # append rows to the table with the same headers
                for t in p["tables"]:
                    tgt = next((x for x in merged["tables"] if x["headers"] == t["headers"]), None)
                    if tgt is not None:
                        tgt["rows"] += t["rows"]
                    else:
                        merged["tables"].append(t)
            last = SHOWING.search(pages[-1]["text"])
            if last and _int(last.group(2)) < _int(last.group(3)):
                merged["incomplete"] = f"only rows 1 to {last.group(2)} of {last.group(3)} could be read"
                print(f"[site] {path}: {merged['incomplete']}", flush=True)
            elif len(pages) > 1 or first_text != merged["text"]:
                print(f"[site] {path}: read all {m.group(3) if m else '?'} rows over {len(pages)} page(s)", flush=True)
            return merged

    # ---------- documents ----------
    async def read_document(self, path):
        """Open a document page, find its file (Download link) and return the PDF's text."""
        data = await self.read(path)
        if data.get("error"):
            return {"path": path, "error": data["error"]}
        files = data.get("files") or []
        out = {"path": path, "title": data.get("title", ""), "files": [f["name"] for f in files], "text": ""}
        for f in files:
            href = f["href"]
            if not href:
                continue
            url = href if href.startswith("http") else config.SITE_URL + (href if href.startswith("/") else "/" + href)
            if not same_origin(url):   # ctx.request may bypass the context route: check the host here (Rule 1)
                continue
            key = "file:" + url
            if key in self.cache:
                out["text"] = self.cache[key]
                break
            try:
                resp = await self.ctx.request.get(url, timeout=config.NAV_TIMEOUT * 1000)
                body = await resp.body()
            except Exception as e:
                out["error"] = f"download failed: {e!r}"[:200]
                continue
            text = ""
            if body[:5] == b"%PDF-":
                text = await asyncio.to_thread(pdf_text, body)
            elif body[:1] in (b"<",):
                text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace"))
            elif b"\x00" not in body[:200]:
                text = body.decode("utf-8", "replace")
            text = re.sub(r"[ \t]+", " ", text).strip()
            if not text:   # say what came back, so the next version can read it
                print(f"[site] no text in {url[:120]}: {len(body)} bytes, starts {body[:12]!r}", flush=True)
            if text:
                self.cache[key] = text
                out["text"] = text
                break
        if not out["text"]:
            out["note"] = ("No file text found. Use look_at_page on this path to see the document image."
                           if data.get("media") else "No file found on this page.")
        return out

    async def look(self, path, max_images=3):
        """Screenshots of a page's visual content (scanned letters, PDFs, images) as JPEG data URLs."""
        import base64
        path = self._normalize(path)
        await self.ready.wait()
        page = await self.pages.get()
        shots = []
        try:
            await page.goto(config.SITE_URL + path, wait_until="commit")
            await self._wait_content(page)
            await asyncio.sleep(0.3)  # let images decode
            els = []
            for fr in page.frames:
                try:
                    for el in await fr.query_selector_all("img, embed, object, canvas"):
                        box = await el.bounding_box()
                        if box and box["width"] >= 120 and box["height"] >= 120:
                            els.append(el)
                except Exception:
                    pass
            for el in els[:max_images]:
                try:
                    b = await el.screenshot(type="jpeg", quality=70, timeout=5000)
                    shots.append("data:image/jpeg;base64," + base64.b64encode(b).decode())
                except Exception:
                    pass
            if not shots:
                b = await page.screenshot(type="jpeg", quality=70, full_page=True)
                shots.append("data:image/jpeg;base64," + base64.b64encode(b).decode())
        except Exception as e:
            print(f"[site] look failed {path}: {e!r}", flush=True)
        finally:
            self.pages.put_nowait(page)
        return shots

    async def screenshot(self, path, height=1400):
        """JPEG data URL of the page as rendered (CDP capture: does not wait on fonts/animations)."""
        path = self._normalize(path)
        await self.ready.wait()
        page = await self.pages.get()
        try:
            await page.set_viewport_size({"width": 1400, "height": height})
            await page.goto(config.SITE_URL + path, wait_until="commit")
            await self._wait_content(page)
            await asyncio.sleep(0.4)  # let web fonts paint (digits are drawn by the page's font)
            cdp = await page.context.new_cdp_session(page)
            shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
            await cdp.detach()
            return "data:image/jpeg;base64," + shot["data"]
        except Exception as e:
            print(f"[site] screenshot failed {path}: {e!r}"[:200], flush=True)
            return None
        finally:
            try:
                await page.set_viewport_size({"width": 1400, "height": 1000})
            except Exception:
                pass
            self.pages.put_nowait(page)

    async def read_many(self, paths):
        return await asyncio.gather(*[self.read(p) for p in paths])

    # ---------- patient search ----------
    async def search_params(self):
        """Names of the patient search's GET fields, read from its form (clinic versions may name them differently).
        {"last": ..., "first": ..., "dob": ..., "query": ...}; missing keys are not offered by the form."""
        if self._search_params is not None:
            return self._search_params
        found = {}
        data = await self.read("/patients", all_pages=False)
        for f in data.get("forms", []):
            if not f.startswith("GET"):
                continue
            for name in re.findall(r"(?:::|\|)\s*([A-Za-z_][\w\-\[\]]*)\(", f):
                low = name.lower()
                if re.search(r"last|surname|family", low):
                    found.setdefault("last", name)
                elif re.search(r"first|given", low):
                    found.setdefault("first", name)
                elif re.search(r"dob|birth|born", low):
                    found.setdefault("dob", name)
                elif re.fullmatch(r"q|query|search|term|name|patient|keyword", low):
                    found.setdefault("query", name)
        if not found:
            found = {"last": "lastName", "dob": "dob"}    # what the practice clinic uses
        self._search_params = found
        print(f"[site] patient search fields: {found}", flush=True)
        return found

    # ---------- providers ----------
    async def providers(self):
        """[{name, value, group}] from the provider select of the schedule's provider view (Active and Inactive)."""
        async with self._prov_lock:
            if self._providers is not None:
                return self._providers
            found = []
            sched = await self.read("/schedule", all_pages=False)
            cands = [h for _, h in sched.get("links", []) if h.startswith("/schedule/providers")]
            for path in (cands[:1] or []) + ["/schedule/providers"]:
                d = await self.read(path.split("?")[0], all_pages=False)
                for s in d.get("selects", []):
                    names = [o for o in s["options"] if re.search(r",\s*(MD|DO|NP|PA|PA-C|RN|DPM|DDS|PhD|LCSW|CNM|OD|DC|PT)\b", o["text"])]
                    if len(names) >= 2:
                        found = [{"name": o["text"], "value": o["value"], "group": o["group"]} for o in names]
                        break
                if found:
                    break
            self._providers = found
            print(f"[site] provider directory: {len(found)} providers "
                  f"({sum(1 for p in found if p['group'].lower().startswith('inactive'))} inactive)", flush=True)
            return found

    async def provider_specialty(self, name):
        """Specialty of a provider, from their schedule page. None when it cannot be found."""
        key = norm_name(name)
        if key in self.specialty_cache:
            return self.specialty_cache[key]
        provs = await self.providers()
        match = [p for p in provs if norm_name(p["name"]) == key]
        if not match:
            base = norm_name(re.sub(r",.*$", "", name))
            match = [p for p in provs if norm_name(re.sub(r",.*$", "", p["name"])) == base]
        spec = None
        if len(match) == 1 and match[0]["value"]:
            v = match[0]["value"]
            path = v if v.startswith("/") else f"/schedule/providers/{v}"
            tries = [path, path]
            iso = _iso_day(self.today)
            if iso and "?" not in path:
                tries.append(f"{path}?date={iso}")
            d = {}
            for attempt, p in enumerate(tries):   # a failed read (rate limit, 503) must not be remembered as "no specialty"
                self.cache.pop(self._normalize(p), None)   # else the retry just gets the same bad page from the cache
                d = await self.read(p, all_pages=False)
                spec = parse_specialty(d.get("text", ""), match[0]["name"])
                if spec:
                    break
                await asyncio.sleep(0.5 + attempt)
            if not spec:
                head = " / ".join(l.strip() for l in d.get("text", "").split("\n") if l.strip())[:400]
                print(f"[site] no specialty for {name!r} at {path}: error={d.get('error')!r} text={head!r}", flush=True)
        if spec or not match:
            self.specialty_cache[key] = spec   # only a found specialty (or an unknown name) is cached
        return spec

    # ---------- rendering for the model ----------
    def learn_nav(self, pages):
        """Links present on every page we have seen are sidebar/navigation: drop them from views."""
        sets = [set(h for _, h in p.get("links", [])) for p in pages if p.get("links")]
        if len(sets) >= 2:
            common = set.intersection(*sets)
            self.nav_links |= {h for h in common if h.count("/") <= 2}

    def _dump(self, data, per_pattern=2, chars=3500):
        """Print the first pages of each kind (path with ids, numbers and query values blanked) in full. The scored
        sets' output is the only place their page layouts can be seen; solvers for new task kinds are built from it."""
        pattern = re.sub(r"=[^&]*", "=", re.sub(r"(?<=/)[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", "*", data["path"]))
        if self.dumped.get(pattern, 0) >= per_pattern or len(self.dumped) > 120:
            return
        self.dumped[pattern] = self.dumped.get(pattern, 0) + 1
        text = self.render(data, max_chars=chars)
        print("[layout] " + text.replace("\n", "\n[layout] "), flush=True)

    async def recon(self, max_pages=45):
        """Practice runs only: walk the app's menus and open a couple of rows of every list, one page at a time
        (the practice site rate-limits), so _dump prints the layout of every kind of page."""
        def pattern(p):
            return re.sub(r"\?.*$", "", re.sub(r"(?<=/)[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", "*", p))

        seen, patterns, queue = set(), set(), ["/"]
        while queue and len(seen) < max_pages:
            path = queue.pop(0)
            if path in seen or pattern(path) in patterns:
                continue
            seen.add(path)
            patterns.add(pattern(path))
            data = await self.read(path, all_pages=False)
            await asyncio.sleep(0.3)
            if data.get("error"):
                continue
            links = [h for _, h in data.get("links", []) if h.startswith("/") and not h.startswith("//")
                     and not re.search(r"log ?out|sign ?out|\.pdf", h, re.I)]
            for t in data.get("tables", []):
                for r in t["rows"][:2]:
                    links += [h for _, h in r["links"] if h.startswith("/")]
            # one example of every kind of link not seen yet: menus, chart tabs, a row of each list, payer pages...
            queue += [h for h in dict.fromkeys(links) if pattern(h) not in patterns]
        print(f"[layout] recon read {len(seen)} pages: {sorted(patterns)}", flush=True)

    def render(self, data, find=None, max_chars=None):
        max_chars = max_chars or config.PAGE_CHARS
        if data.get("error"):
            return f"=== {data['path']} ===\nERROR: {data['error']}"
        text = re.sub(r"\n{3,}", "\n\n", data["text"]).strip()
        if find:
            pats = [p.strip().lower() for p in find.split("|") if p.strip()]
            lines = text.split("\n")
            keep = set(range(min(6, len(lines))))
            for i, ln in enumerate(lines):
                low = ln.lower()
                if ln.startswith("## ") or any(p in low for p in pats):
                    keep.update({i - 1, i, i + 1} if not ln.startswith("## ") else {i})
            text = "\n".join(lines[i] for i in sorted(k for k in keep if 0 <= k < len(lines)))
            text = f"[showing lines matching: {find}]\n" + text
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[truncated, {len(data['text'])} chars total; use find= to filter]"
        in_tables = {h for t in data.get("tables", []) for r in t["rows"] for _, h in r["links"]}
        links = [(t, h) for t, h in data.get("links", []) if h not in self.nav_links and h not in in_tables]
        if find:
            pats = [p.strip().lower() for p in find.split("|") if p.strip()]
            links = [(t, h) for t, h in links if any(p in t.lower() for p in pats)] or links[:15]
        link_lines = "\n".join(f"- {t} -> {h}" for t, h in links[:60])
        if len(links) > 60:
            link_lines += f"\n- ...{len(links) - 60} more links"
        forms = "\n".join(data.get("forms", []))
        if data.get("files"):
            forms += ("\n" if forms else "") + "NOTE: this page has a document file. Call read_document on this path to read its text."
        elif data.get("media"):
            forms += ("\n" if forms else "") + "NOTE: this page shows visual content (" + "; ".join(data["media"][:4]) + \
                     "). Its text may not be in the page text: use look_at_page on this path to see it."
        out = f"=== {data['path']} | {data.get('title', '')} ===\n"
        if data.get("incomplete"):
            out += f"WARNING: list incomplete, {data['incomplete']}.\n"
        out += text
        if link_lines:
            out += f"\n--- other links ---\n{link_lines}"
        if forms:
            out += f"\n--- forms (GET forms = query parameters on the URL) ---\n{forms}"
        return out

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass


def norm_name(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"^(dr\.?|doctor)\s+", "", s.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9,]+", " ", s.lower()).replace(" ,", ",").strip()


TRANSIENT = re.compile(r"\b(50[234]\b|Service (Temporarily )?Unavailable|Bad Gateway|Gateway Time-?out|Too Many Requests|"
                       r"429\b)", re.I)
NOTICE = re.compile(r"\b(acknowledge|notice|consent|terms of use|confidentiality|authorized use|before you continue)\b", re.I)
NOTICE_BTN = re.compile(r"^\s*(acknowledge|i acknowledge|i agree|agree|accept|i understand|continue|proceed|ok)\b", re.I)


def parse_specialty(text, name):
    """Provider page: '<Name>' then '<Specialty> · <Weekday>, <Month> <d>, <yyyy>'."""
    lines = [ln.strip() for ln in text.split("\n")]
    for ln in lines:
        m = re.match(r"^(.+?)\s+[·•|-]\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*day,", ln)
        if m and len(m.group(1)) < 60:
            return m.group(1).strip()
    m = re.search(r"Specialty\s*\n\s*([^\n]+)", text)
    if m:
        return m.group(1).strip()
    # looser: the first non-empty line after the provider's name, '<Specialty> · <anything>' or just '<Specialty>'
    key = norm_name(name)
    for i, ln in enumerate(lines):
        if ln and norm_name(ln) == key:
            nxt = next((l for l in lines[i + 1:i + 4] if l), "")
            cand = re.split(r"\s+[·•|]\s+", nxt)[0].strip()
            if cand and len(cand) < 50 and not re.search(r"\d|^Provider$|,", cand):
                return cand
            break
    return None


def _iso_day(today):
    """'Oct 14, 2026' -> '2026-10-14' (None if it does not parse)."""
    import datetime as _dt
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return _dt.datetime.strptime(today.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


def pdf_text(data):
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages[:10])
    except Exception as e:
        return f"(could not read PDF: {e!r})"[:200]
