"""Deterministic helpers over Wrenfield pages, exposed to the model as tools.

They are faster and more exact than letting the model page through charts: one call finds a patient by name and
date of birth, gathers every appointment (list + each date of recurring series), or applies a new-vs-established
rule. Each returns plain facts plus warnings; the model still decides the final answer and its form.
"""
import asyncio
import datetime as dt
import json
import re
import unicodedata

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DT_RE = re.compile(r"([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),\s*(\d{4})(?:,?\s+(?:at\s+)?(\d{1,2}:\d{2})\s*([AP]M)?\s*([A-Z]{2,4})?)?")
TZ_OFFSETS = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
              "AKST": -9, "AKDT": -8, "HST": -10, "UTC": 0, "GMT": 0}
STATE_TZ = {"KS": "America/Chicago", "MO": "America/Chicago", "NE": "America/Chicago", "OK": "America/Chicago",
            "TX": "America/Chicago", "IA": "America/Chicago", "IL": "America/Chicago", "MN": "America/Chicago",
            "WI": "America/Chicago", "CO": "America/Denver", "NM": "America/Denver", "WY": "America/Denver",
            "UT": "America/Denver", "MT": "America/Denver", "AZ": "America/Phoenix", "CA": "America/Los_Angeles",
            "WA": "America/Los_Angeles", "OR": "America/Los_Angeles", "NV": "America/Los_Angeles",
            "NY": "America/New_York", "MI": "America/Detroit", "OH": "America/New_York", "FL": "America/New_York"}


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", fold(s)).strip()


def parse_dt(s):
    """'Mar 4, 2026, 2:00 PM CST' -> (date, 'HH:MM' or None, 'CST' or None). None if no date."""
    m = DT_RE.search(s or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    if not mon:
        return None
    d = dt.date(int(m.group(3)), mon, int(m.group(2)))
    hhmm = None
    if m.group(4):
        h, mi = map(int, m.group(4).split(":"))
        ap = m.group(5)
        if ap == "PM" and h != 12:
            h += 12
        if ap == "AM" and h == 12:
            h = 0
        hhmm = f"{h:02d}:{mi:02d}"
    return d, hhmm, m.group(6)


def parse_any_date(s):
    """YYYY-MM-DD, MM/DD/YYYY or 'Mon d, yyyy' -> date."""
    s = (s or "").strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    p = parse_dt(s)
    return p[0] if p else None


def local_date(d, hhmm, abbr, location, site):
    """Calendar date at the appointment's location. The product shows each time with a zone abbreviation; if that
    zone is not the location's zone, convert."""
    if not (hhmm and abbr and abbr in TZ_OFFSETS and location):
        return d
    place = (getattr(site, "locations", {}) or {}).get(location, "")
    st = place[-2:] if re.search(r",\s*[A-Z]{2}$", place) else None
    zone = STATE_TZ.get(st or "")
    if not zone:
        return d
    try:
        from zoneinfo import ZoneInfo
        h, mi = map(int, hhmm.split(":"))
        shown = dt.datetime(d.year, d.month, d.day, h, mi, tzinfo=dt.timezone(dt.timedelta(hours=TZ_OFFSETS[abbr])))
        return shown.astimezone(ZoneInfo(zone)).date()
    except Exception:
        return d


def col_index(headers, *keys):
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(k in hl for k in keys):
            return i
    return None


# ---------------------------------------------------------------- patients
def _patient_rows(data):
    out = []
    for t in data.get("tables", []):
        for r in t["rows"]:
            cells = r["cells"]
            hrefs = [h for _, h in r["links"] if re.match(r"^/patients/[^/?#]+$", h)]
            if not hrefs or not cells:
                continue
            dob = next((c for c in cells if re.search(r"\d{2}/\d{2}/\d{4}", c)), "")
            mrn = next((c for c in cells if re.fullmatch(r"[A-Z]{1,4}\d{5,}", c)), "")
            flags = [f for f in ("Merged", "Deleted", "Restricted", "Deceased", "Inactive", "Test record") if any(f.lower() in c.lower() for c in cells[1:])]
            out.append({"name": cells[0], "mrn": mrn, "dob": (re.search(r"\d{2}/\d{2}/\d{4}", dob).group() if dob else ""),
                        "flags": flags, "path": hrefs[0]})
    return out


def _name_match(row_name, last, first):
    rn = fold(row_name)
    if "," in rn:
        rl, rf = [x.strip() for x in rn.split(",", 1)]
    else:
        parts = rn.split()
        rf, rl = " ".join(parts[:-1]), (parts[-1] if parts else "")
    ok = True
    if last:
        ok = ok and norm(rl) == norm(last)
    if first:
        f = norm(first)
        rfn = norm(rf)
        ok = ok and (rfn == f or rfn.split(" ")[:1] == f.split(" ")[:1])
    return ok


async def find_patient(site, last_name=None, first_name=None, dob=None):
    """Search by date of birth (exact) and/or last name; return matching chart rows with flags."""
    tried, rows, notes = [], [], []
    d = parse_any_date(dob) if dob else None
    sp = await site.search_params() if hasattr(site, "search_params") else {"last": "lastName", "dob": "dob"}
    last_key = sp.get("last") or sp.get("query")
    if d and sp.get("dob"):
        path = f"/patients?{sp['dob']}={d.isoformat()}"
        data = await site.read(path)
        # DIAG-01 evidence: one hit or miss per distinct date-of-birth search, counted at the read. A task whose patient
        # has no chart makes pick_patient search up to 5 more names with the same DOB; counted per call that would be
        # six misses, so one odd task could fake a search failure on a clean clinic. Counting at the read also survives
        # a wait_for that cancels the rest of this call. Fake sites in tests have no dob_searches and are not counted.
        seen = getattr(site, "dob_searches", None)
        if seen is not None and path not in seen:
            seen.add(path)
            if _patient_rows(data):
                site.search_hits += 1
            else:
                site.search_misses += 1
        tried.append(path)
        if data.get("incomplete"):
            notes.append(f"{path}: {data['incomplete']}")
        rows = [r for r in _patient_rows(data) if _name_match(r["name"], last_name, first_name)]
        if not rows and (last_name or first_name):
            # same birth date, name spelled differently: show what is there
            others = _patient_rows(data)
            if others:
                notes.append("Patients born that day (names did not match exactly): " +
                             "; ".join(f"{o['name']} {o['dob']} {o['path']} {' '.join(o['flags'])}" for o in others[:10]))
    if not rows and last_name and last_key:
        for ln in dict.fromkeys([last_name.strip(), fold(last_name).strip()]):
            path = f"/patients?{last_key}={ln}"
            data = await site.read(path)
            tried.append(path)
            if data.get("incomplete"):
                notes.append(f"{path}: {data['incomplete']}")
            cand = [r for r in _patient_rows(data) if _name_match(r["name"], last_name, first_name)]
            if d:
                cand = [r for r in cand if r["dob"] == d.strftime("%m/%d/%Y")]
            if cand:
                rows = cand
                break
    if not getattr(site, "location_ok", True):
        notes.append("WARNING: the working location could not be confirmed as All locations; search may miss patients.")
    active = [r for r in rows if "Deleted" not in r["flags"]]
    if len({r["mrn"] for r in active if "Merged" not in r["flags"]}) > 1:
        notes.append("More than one active chart matches: use another detail from the task to choose, or skip.")
    return {"matches": rows, "searched": tried, "notes": notes}


async def find_patient_anywhere(site, last_name, dob=None, max_charts=6):
    """Charts reached without the patient search, for when the working location could not be set to All locations
    (the patient search only shows the working location's patients). The documents list and the billing list have
    their own location filter, empty = all locations: their rows lead to charts, whose header gives name, MRN and DOB."""
    ln = (last_name or "").strip()
    if not ln:
        return []
    want = norm(ln)
    pages = await site.read_many([f"/documents?patient={ln}", f"/billing?patient={ln}"])
    charts, invoices = [], []
    for data in pages:
        for t in data.get("tables", []):
            for r in t["rows"]:
                if not any(want and f" {want} " in f" {norm(c)} " for c in r["cells"]):
                    continue
                for _, h in r["links"]:
                    h = h.split("?")[0]
                    if re.match(r"^/patients/[^/]+$", h):
                        charts.append(h)
                    elif re.match(r"^/billing/invoices/[^/]+$", h):
                        invoices.append(h)
    if invoices and len(set(charts)) < max_charts:
        for data in await site.read_many(list(dict.fromkeys(invoices))[:3]):
            charts += [h for _, h in data.get("links", []) if re.match(r"^/patients/[^/?#]+$", h)]
    out = []
    for data in await site.read_many(list(dict.fromkeys(charts))[:max_charts]):
        txt = data.get("text", "")
        name = re.sub(r"\s*·.*$", "", data.get("title", "")).strip()
        m_dob = re.search(r"Date of birth\s*\n?\s*(\d{2}/\d{2}/\d{4})", txt)
        m_mrn = re.search(r"MRN\s*\n?\s*([A-Z]{1,4}\d{5,})", txt)
        if not name or not m_dob:
            continue
        flags = [f for f, pat in (("Restricted", r"chart is restricted"), ("Merged", r"was merged into"),
                                  ("Deleted", r"record (?:was|is) deleted"), ("Test record", r"test record"))
                 if re.search(pat, txt[:1500], re.I)]
        out.append({"name": name, "mrn": m_mrn.group(1) if m_mrn else "", "dob": m_dob.group(1), "flags": flags,
                    "path": data["path"].split("?")[0]})
    if dob:
        d = parse_any_date(dob)
        out = [r for r in out if d and r["dob"] == d.strftime("%m/%d/%Y")]
    return out


# ---------------------------------------------------------------- appointments
def _appointment_table_rows(data):
    rows = []
    for t in data.get("tables", []):
        h = t["headers"]
        if h:
            di, si = col_index(h, "date"), col_index(h, "status")
            if di is None or si is None or col_index(h, "change") is not None:
                continue
            ti, pi, li = col_index(h, "type", "visit", "service", "reason"), col_index(h, "provider", "clinician"), col_index(h, "location", "site")
        else:
            di, ti, pi, li, si = 0, 1, 2, 3, 4
        for r in t["rows"]:
            c = r["cells"]
            get = lambda i: c[i] if i is not None and i < len(c) else ""
            p = parse_dt(get(di))
            if not p:
                continue
            rows.append({"when": get(di), "date": p[0], "time": p[1], "tz": p[2], "type": get(ti), "provider": get(pi),
                         "location": get(li), "status": get(si), "links": [x for _, x in r["links"]]})
    return rows


def _field(text, label):
    m = re.search(rf"(?:^|\n){re.escape(label)}\s*\n\s*([^\n]+)", text)
    return m.group(1).strip() if m else ""


async def _recurring(site, path):
    data = await site.read(path)
    text = data.get("text", "")
    typ, prov, loc = _field(text, "Type"), _field(text, "Provider"), _field(text, "Location")
    out = []
    for t in data.get("tables", []):
        h = [x.lower() for x in t["headers"]]
        if h and (not any("status" in x for x in h) or any("change" in x for x in h)):
            continue
        for r in t["rows"]:
            c = r["cells"]
            p = parse_dt(c[0] if c else "")
            if not p:
                continue
            status = c[col_index(t["headers"], "status")] if t["headers"] and col_index(t["headers"], "status") is not None and col_index(t["headers"], "status") < len(c) else (c[-1] if len(c) > 1 else "")
            out.append({"when": c[0], "date": p[0], "time": p[1], "tz": p[2], "type": typ, "provider": prov,
                        "location": loc, "status": status, "source": "recurring " + path})
    return out, data.get("incomplete")


async def appointment_history(site, patient_paths):
    """Every appointment of the patient across the given chart paths: the Appointments tab rows plus each date of
    every recurring series, with the status that date has. Dates are calendar dates at the appointment's location."""
    paths = []
    for p in patient_paths:
        p = p.split("?")[0].rstrip("/")
        m = re.match(r"^(/patients/[^/]+)", p)
        if m:
            paths.append(m.group(1))
    paths = list(dict.fromkeys(paths))
    warnings, rows, rec_paths, banners = [], [], [], []
    charts = await site.read_many([p + "/appointments" for p in paths])
    for p, data in zip(paths, charts):
        if data.get("error"):
            warnings.append(f"{p}: {data['error']}")
            continue
        if data.get("incomplete"):
            warnings.append(f"{p}/appointments: {data['incomplete']}")
        txt = data.get("text", "")
        if re.search(r"restricted", txt[:300], re.I) and "Give a reason" in txt:
            warnings.append(f"{p}: chart is restricted; appointments not visible")
        for b in re.findall(r"[^\n]*merged into this record[^\n]*", txt):
            banners.append(b.strip())
        for r in _appointment_table_rows(data):
            r["source"] = "chart " + p
            rows.append(r)
        rec_paths += [h for _, h in data.get("links", []) if "/recurring/" in h]
    rec_paths = list(dict.fromkeys(x.split("?")[0] for x in rec_paths))
    for res in await asyncio.gather(*[_recurring(site, rp) for rp in rec_paths]):
        rr, inc = res
        if inc:
            warnings.append(f"recurring series: {inc}")
        rows += rr
    # calendar date at the location; de-duplicate (a recurring date may also be listed on the chart)
    final = {}
    for r in rows:
        r["date"] = local_date(r["date"], r["time"], r["tz"], r["location"], site)
        key = (r["date"], r["time"], norm(r["provider"]), norm(r["type"]))
        if key in final and not r["source"].startswith("recurring"):
            continue
        final[key] = r
    out = sorted(final.values(), key=lambda r: (r["date"], r["time"] or ""), reverse=True)
    return {"appointments": out, "recurring_series": rec_paths, "merged_banners": banners, "warnings": warnings}


def window(visit, years):
    try:
        anniv = visit.replace(year=visit.year - years)
    except ValueError:  # Feb 29 -> Feb 28
        anniv = visit.replace(year=visit.year - years, day=28)
    return anniv + dt.timedelta(days=1), visit - dt.timedelta(days=1)


async def new_or_established(site, patient_paths, provider, visit_date, excluded_services, years=3,
                             counting_statuses=("Completed",)):
    visit = parse_any_date(visit_date)
    if not visit:
        return {"error": f"could not read visit date {visit_date!r}"}
    start, end = window(visit, int(years))
    hist, target_spec = await asyncio.gather(appointment_history(site, patient_paths), site.provider_specialty(provider))
    appts = hist["appointments"]
    provs = sorted({a["provider"] for a in appts if a["provider"]})
    specs = dict(zip(provs, await asyncio.gather(*[site.provider_specialty(p) for p in provs])))
    excl = {norm(x) for x in excluded_services or []}
    statuses = {norm(x) for x in counting_statuses}
    tgt = norm(re.sub(r"^(dr\.?\s+)", "", provider, flags=re.I))
    counting, ignored, uncertain = [], [], []
    for a in appts:
        same_prov = norm(a["provider"]) == tgt or norm(a["provider"].split(",")[0]) == norm(provider.split(",")[0])
        spec = specs.get(a["provider"])
        in_window = start <= a["date"] <= end
        status_ok = norm(a["status"]) in statuses
        svc_ok = norm(a["type"]) not in excl
        spec_ok = same_prov or (spec is not None and target_spec is not None and norm(spec) == norm(target_spec))
        rec = {"date": a["date"].isoformat(), "type": a["type"], "provider": a["provider"], "specialty": spec,
               "status": a["status"], "location": a["location"], "source": a["source"]}
        if in_window and status_ok and svc_ok and spec_ok:
            counting.append(rec)
        elif in_window and status_ok and svc_ok and not same_prov and (spec is None or target_spec is None):
            uncertain.append(rec)
        elif in_window and (same_prov or spec_ok):
            ignored.append({**rec, "why": "status" if not status_ok else "excluded service"})
    counting.sort(key=lambda r: r["date"], reverse=True)
    warnings = list(hist["warnings"])
    if target_spec is None:
        warnings.append(f"specialty of {provider!r} not found in the provider directory")
    if uncertain:
        warnings.append("some appointments could count but their provider's specialty is unknown: see 'uncertain'")
    if not getattr(site, "location_ok", True):
        warnings.append("working location not confirmed as All locations: appointments at other locations may be missing")
    if not appts:
        warnings.append("no appointment rows were read: open the chart's Appointments tab to check it really has none")
    last = counting[0]["date"] if counting else None
    return {
        "result": ("ESTABLISHED:" + last) if last else "NEW",
        "most_recent_counting_date": last,
        "provider_specialty": target_spec,
        "window": f"{start.isoformat()} through {end.isoformat()}",
        "appointments_total": len(appts),
        "counting": counting[:5],
        "in_window_not_counting": ignored[:8],
        "uncertain": uncertain[:8],
        "merged_banners": hist["merged_banners"],
        "recurring_series": len(hist["recurring_series"]),
        "warnings": warnings,
        "next_step": "If safe_to_answer is true, submit now in the rule's answer form; do not re-check providers.",
        "safe_to_answer": bool(target_spec is not None and not uncertain and appts and getattr(site, "location_ok", True)
                               and not any(k in w for w in hist["warnings"] for k in ("could be read", "restricted", "error", "did not load"))),
    }


# ---------- contact details (phone digits in the page TEXT are scrambled; the screen is right) ----------
PHONE_RE = re.compile(r"Phone\s*\n\s*([^\n]*\d[^\n]*)")


def _digits(s):
    return re.sub(r"\D", "", s or "")


def substitution_consistent(dom, screen):
    """True if screen digits can come from dom digits by one fixed digit-for-digit substitution."""
    if len(dom) != len(screen) or not dom:
        return False
    fwd, back = {}, {}
    for a, b in zip(dom, screen):
        if fwd.setdefault(a, b) != b or back.setdefault(b, a) != a:
            return False
    return True


async def patient_contact(site, llm, patient_path):
    """Phone (and other contact lines) as the screen shows them, checked against the page text."""
    path = "/" + "/".join(site._normalize(patient_path).split("?")[0].strip("/").split("/")[:2])
    data = await site.read(path, all_pages=False)
    text = data.get("text", "")
    m = PHONE_RE.search(text)
    dom_phone = m.group(1).strip() if m else ""
    out = {"path": path, "page_text_phone_UNRELIABLE": dom_phone}
    # Fast path: decode with the substitution learned earlier this session.
    dd = _digits(dom_phone)
    decoded = None
    if dd and site.digit_map_ok and all(c in site.digit_map for c in dd):
        decoded = "".join(site.digit_map[c] for c in dd)
    site.phone_reads += 1
    if decoded and site.digit_map_hits >= 3 and site.phone_reads % 8:
        # The map agreed with 3 screenshots already: decode without a screenshot and a vision call.
        it = iter(decoded)
        phone = re.sub(r"\d", lambda _: next(it), dom_phone)
        print(f"[contact] {path} page_text={dom_phone!r} decoded={phone!r} (trusted map)", flush=True)
        return {**out, "phone": phone, "safe_to_answer": True, "checks": {"learned_map": "trusted"}}
    shot = await site.screenshot(path)
    screen = ""
    for attempt in range(2 if shot else 0):   # an empty or cut-off read gets one retry
        msg, _, _ = await llm.chat([{"role": "user", "content": [
            {"type": "text", "text": "This is a clinic chart page. Transcribe EXACTLY what is displayed under Contact: "
                                     "the Phone line, the Email line and the Address lines. Copy every digit and "
                                     "character as drawn on screen. Reply with JSON only: {\"phone\": ..., \"email\": ..., \"address\": ...}"},
            {"type": "image_url", "image_url": {"url": shot}}]}], max_tokens=3000, timeout=25)
        if msg.get("_finish") == "length":
            print(f"[contact] screen read cut off (attempt {attempt + 1})", flush=True)
            continue
        raw = (msg.get("content") or "").strip()
        mm = re.search(r"\{.*\}", raw, re.S)
        try:
            vis = json.loads(mm.group(0)) if mm else {}
        except Exception:
            vis = {}
        screen = str(vis.get("phone") or "")
        out["screen"] = vis
        if screen:
            break
        print(f"[contact] empty screen read (attempt {attempt + 1}): {raw[:120]!r}", flush=True)
    sd = _digits(screen)
    ok = substitution_consistent(dd, sd) if dd else bool(sd)
    if ok and dd:
        for a, b in zip(dd, sd):
            if site.digit_map.get(a, b) != b:
                site.digit_map_ok = False  # the scramble is not session-wide: stop using the learned map
            site.digit_map[a] = b
    if decoded and sd and decoded != sd:
        site.digit_map_ok = False
        site.digit_map_hits = -10 ** 6   # never trust the map again this session
    elif decoded and decoded == sd:
        site.digit_map_hits += 1
    agree = bool(sd) and ok and (decoded is None or decoded == sd)
    out["phone"] = screen if agree else None
    out["checks"] = {"screen_digits_consistent_with_page_text": ok, "matches_learned_map": None if decoded is None else decoded == sd}
    out["safe_to_answer"] = agree
    if not agree:
        out["warning"] = "Could not read the phone reliably from the screen; skip unless another source confirms it."
    print(f"[contact] {path} page_text={dom_phone!r} screen={screen!r} learned={decoded!r} safe={agree}", flush=True)
    return out
