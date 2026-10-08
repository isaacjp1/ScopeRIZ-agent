"""A fake Wrenfield that copies the structure seen on the practice clinic (Wrenfield 4.2.0), a fake queue and a
scripted fake model, to test the agent end to end with a real browser and no internet.

    python test/mock_wrenfield.py            # serves site :8811, queue :8812, model :8813
    QUEUE_URL=http://127.0.0.1:8812 SITE_URL=http://127.0.0.1:8811 LLM_BASE_URL=http://127.0.0.1:8813/v1 \
      LLM_API_KEY=x SHIFT_SECONDS=1200 MODEL=fake python -m agent.main

What it imitates:
- shell page (header with a location switcher whose menu opens on click, sidebar) + content iframe /v/<random>/;
  the document viewer is one more nested iframe with a Download link to a text PDF and a page image;
- working location, rows per page and page number are session state changed with POST + reload;
- patient search scoped to the working location; Merged / Deleted / Restricted flags; merged duplicate banner;
- chart Appointments tab + recurring series pages ("All dates" with each date's status, "Changed dates");
- provider pages "<Name>" / "<Specialty> · <Weekday>, <date>" and a provider select with Active/Inactive groups.
"""
import html
import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

LOCS = [("Northgate", "Harbor Glen, KS"), ("Front Range", "Summit Hollow, CO"), ("Eastside", "Harbor Glen, KS")]
PROVIDERS = [  # id, name, specialty, active
    ("pr1", "Logan Brown, MD", "Internal Medicine", True),
    ("pr2", "Frances Ward, NP", "Internal Medicine", True),
    ("pr3", "Sharon Morales, MD", "Dermatology", True),
    ("pr4", "Kenneth Sullivan, NP", "Family Medicine", True),
    ("pr5", "Old Timer, MD", "Family Medicine", False),   # left the group
    ("pr6", "Douglas Hughes, MD", "Cardiology", True),
]
P = {}


def add(pid, last, first, dob, loc, phone, **kw):
    P[pid] = dict(id=pid, last=last, first=first, dob=dob, loc=loc, phone=phone, mrn="HG%07d" % (1000000 + len(P)),
                  flags=kw.get("flags", []), appts=kw.get("appts", []), rec=kw.get("rec", []), docs=kw.get("docs", []),
                  merged_from=kw.get("merged_from"))


# Two Carol Jones: the one in the task (born 1982-09-07) is at Front Range (not visible from Northgate).
add("cj1", "Jones", "Carol", "09/07/1982", "Front Range", "(785) 310-6041")
add("cj2", "Jones", "Carol", "03/15/1975", "Northgate", "(352) 810-4091")
for i, fn in enumerate(["Ahmed", "Amanda", "Amber", "Amy", "Arthur", "Ashley", "Austin", "Betty", "Beverly", "Brittany", "Charles"]):
    add(f"j{i}", "Jones", fn, "01/%02d/1970" % (i + 1), "Northgate", "(620) 555-01%02d" % i,
        flags=(["Deleted"] if fn == "Arthur" else []))
add("rs", "Jones", "Catherine", "09/30/1950", "Northgate", "(620) 555-9999", flags=["Restricted"])
# Judith Jackson: established 2025-07-21 for Kenneth Sullivan (Family Medicine) on 2028-07-20.
add("jj", "Jackson", "Judith", "11/12/2004", "Northgate", "(620) 555-1212", appts=[
    ("Jul 21, 2025, 10:00 AM CDT", "Office visit", "Old Timer, MD", "Northgate", "Completed"),      # inactive, FM: counts
    ("Jul 25, 2025, 9:00 AM CDT", "Lab draw", "Kenneth Sullivan, NP", "Northgate", "Completed"),    # excluded service
    ("Aug 2, 2025, 9:00 AM CDT", "Office visit", "Kenneth Sullivan, NP", "Northgate", "No-show"),   # status
    ("Jul 1, 2025, 9:00 AM CDT", "Office visit", "Sharon Morales, MD", "Northgate", "Completed"),   # other specialty
    ("Jul 19, 2025, 9:00 AM CDT", "Office visit", "Kenneth Sullivan, NP", "Northgate", "Completed"),  # outside window
])
# Peter Henry: only a recurring cardiology series; its last date is cancelled; visit 2026-12-25 with Hughes.
add("ph", "Henry", "Peter", "06/23/1975", "Eastside", "(620) 555-3434", rec=[
    dict(id="r1", rule="Every week on Monday at 09:00", type="Cardiac rehab", provider="Douglas Hughes, MD", location="Eastside",
         dates=[("Jan 12, 2026, 9:00 AM CST", "Cancelled"), ("Jan 5, 2026, 9:00 AM CST", "Completed"),
                ("Dec 29, 2025, 9:00 AM CST", "Completed")],
         changed=[("Jan 12, 2026", "Cancelled", "Jan 2, 2026, 1:00 PM CST")])])
# Laura Torres: a duplicate record merged into her chart holds the counting visit. Visit with Elena Gutierrez (FM).
add("lt", "Torres", "Laura", "02/20/2001", "Front Range", "(719) 555-1000", appts=[
    ("Aug 15, 2025, 12:30 AM CDT", "Office visit", "Kenneth Sullivan, NP", "Front Range", "Completed")  # = Aug 14 in Colorado,
])
add("ltdup", "Torres", "Laura", "02/20/2001", "Front Range", "(719) 555-1000", flags=["Merged"], merged_from="lt")
# Gregory Porter: referral reason is only in the PDF.
add("gp", "Porter", "Gregory", "04/08/1976", "Northgate", "(620) 555-7777",
    docs=[dict(id="d1", received="Sep 16, 2025", type="Referral letter", title="Referral letter from Granite Fork Internal Medicine",
               body=["Granite Fork Internal Medicine", "Re: Gregory Porter, DOB 1976-04-08",
                     "Reason for referral: Chronic cough lasting more than eight weeks", "Sincerely, Dr. A. Smith"])])
PROVIDERS.append(("pr7", "Elena Gutierrez, NP", "Family Medicine", True))

LAST_CHART = ["cj1"]  # chart shown last (the mock vision model "reads" its phone)
CALLS = []           # first 60 chars of every model request, for checking how many calls a run made
SESS = {}  # cookie sid -> {"loc": ..., "size": 5, "page": {key: n}}


def sess(h):
    m = re.search(r"sid=(\w+)", h.headers.get("Cookie", ""))
    return SESS.get(m.group(1)) if m else None


def make_pdf(lines):
    """Minimal one-page PDF with Helvetica text (extractable by pypdf)."""
    content = "BT /F1 12 Tf 72 720 Td 16 TL " + " ".join(
        "(" + l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF"
    return out.encode("latin-1")


def shell(s):
    cur = s["loc"]
    opts = '<button type="button" aria-pressed="%s" onclick="setloc(\'all\')"><span></span><span>All locations</span></button>' % (
        "true" if cur == "all" else "false")
    for n, place in LOCS:
        opts += ('<button type="button" aria-pressed="%s" onclick="setloc(\'%s\')"><span></span><span><span>%s</span>'
                 '<span>%s</span></span></button>') % ("true" if cur == n else "false", n, n, place)
    label = "All locations" if cur == "all" else cur
    tok = "%x" % random.getrandbits(48)
    return f"""<!doctype html><html><head><title>Wrenfield</title><style>#menu{{display:none}} #menu.open{{display:block}}
iframe{{width:1200px;height:900px;border:0}}</style></head><body>
<a href="#main">Skip to main content</a><img src="http://blocked.example/x.png" alt="">
<nav><a href="/">Dashboard</a> <a href="/patients">Patients</a> <a href="/schedule">Schedule</a> <a href="/documents">Documents</a></nav>
<header><div role="button" aria-haspopup="true" onclick="document.getElementById('menu').classList.toggle('open')">
<span>Location</span><br><span>{label}</span></div>
<div id="menu"><div>Work at location</div>{opts}</div> Harbor Glen Medical Group · Wrenfield 4.2.0</header>
<p id="ld">Loading Wrenfield</p>
<main id="main"><iframe src="/v/{tok}/"></iframe></main>
<script>function setloc(l){{fetch('/setloc?loc='+encodeURIComponent(l),{{method:'POST'}}).then(()=>location.reload())}}</script>
</body></html>"""


FRAME = """<!doctype html><html><head><style>body{font-family:sans-serif} img{width:600px;height:780px}</style></head>
<body><p>...</p><script>
setTimeout(async () => {
  const src = window.frameElement && window.frameElement.dataset.doc;
  const p = src ? '/docview/' + src : window.parent.location.pathname + window.parent.location.search;
  const r = await fetch('/data?p=' + encodeURIComponent(p));
  document.body.innerHTML = await r.text();
  for (const s of document.querySelectorAll('script')) { const n = document.createElement('script'); n.textContent = s.textContent; s.replaceWith(n); }
  try { window.parent.document.getElementById('ld').textContent = ''; } catch (e) {}
}, 120);
</script></body></html>"""

PAGER_JS = """<script>
function pg(k,v){fetch('/setpage?key='+encodeURIComponent(k)+'&page='+v,{method:'POST'}).then(()=>window.top.location.reload())}
function sz(v){fetch('/setsize?size='+v,{method:'POST'}).then(()=>window.top.location.reload())}
</script>"""


def paged(s, key, rows_html, header):
    size = s["size"]
    n = len(rows_html)
    pages = max(1, (n + size - 1) // size)
    cur = min(s["page"].get(key, 1), pages)
    a = (cur - 1) * size
    body = "".join(rows_html[a:a + size])
    sel = "".join(f'<option value="{v}" {"selected" if v == size else ""}>{v} per page</option>' for v in (5, 10))
    btns = f'<button {"disabled" if cur == 1 else ""} onclick="pg(\'{key}\',{cur - 1})">Previous</button>' + "".join(
        f'<button onclick="pg(\'{key}\',{i})">{i}</button>' for i in range(1, pages + 1)) + \
        f'<button {"disabled" if cur == pages else ""} onclick="pg(\'{key}\',{cur + 1})">Next</button>'
    foot = f'<div>Showing {a + 1 if n else 0} to {min(a + size, n)} of {n} patients</div>' if n else ""
    pager = f'<select name="size" onchange="sz(this.value)">{sel}</select> {btns}' if n > 5 or size != 5 else ""
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>{foot}{pager}{PAGER_JS}"


def chart_head(p):
    tabs = "".join(f'<a href="/patients/{p["id"]}{t}">{n}</a> ' for n, t in
                   [("Demographics", ""), ("Documents", "/documents"), ("Appointments", "/appointments"), ("Activity", "/activity")])
    flag = " <span>Deleted</span>" if "Deleted" in p["flags"] else ""
    return f'<h1>{p["last"]}, {p["first"]}{flag}</h1><div>MRN</div><div>{p["mrn"]}</div><div>Date of birth</div><div>{p["dob"]}</div><nav>{tabs}</nav>'


def content(s, path):
    u = urlparse(path)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path == "/":
        return "<h1>Dashboard</h1><p>Oct 14, 2026</p><p>Appointments today 74</p>"
    if u.path == "/patients":
        last, dob = q.get("lastName", "").lower(), q.get("dob", "")
        if not last and not dob:
            return "<h1>Patients</h1><form><input name='lastName'><input name='dob' type='date'></form><p>Search to find a patient</p>"
        if os.environ.get("MOCK_BREAK_SEARCH"):   # the search form is there, but no search finds anyone
            return "<h1>Patients</h1><p>No patients match</p>"
        rows = []
        for p in sorted(P.values(), key=lambda x: (x["last"], x["first"])):
            if s["loc"] != "all" and p["loc"] != s["loc"]:
                continue
            mm, dd, yy = p["dob"].split("/")
            if (last and p["last"].lower() == last) or (dob and dob == f"{yy}-{mm}-{dd}"):
                target = p["merged_from"] or p["id"]
                rows.append(f'<tr><td><a href="/patients/{p["id"]}">{p["last"]}, {p["first"]}</a></td><td>{p["mrn"]}</td>'
                            f'<td>{p["dob"]} (40 y)</td><td>F</td><td>{" ".join(p["flags"])}</td></tr>')
        if not rows:
            return "<h1>Patients</h1><p>No patients match</p>"
        return "<h1>Patients</h1><h2>Results</h2>" + paged(s, "patients", rows, "<th>Patient</th><th>MRN</th><th>Date of birth</th><th>Sex</th><th></th>")
    m = re.match(r"^/patients/(\w+)(/\w+)?(/\w+)?$", u.path)
    if m:
        p = P.get(m.group(1))
        if not p:
            return "This page is not available"
        banner = ""
        if p["merged_from"]:
            banner = f'<p>The record with MRN {p["mrn"]} was merged into this record</p><p>What was filed under it is shown here.</p>'
            p = P[p["merged_from"]]
        if "Restricted" in p["flags"]:
            return f"<h1>This chart is restricted</h1><p>MRN {p['mrn']}</p><p>Give a reason to open it.</p>"
        tab = m.group(2) or ""
        head = banner + chart_head(p)
        if tab == "":
            LAST_CHART[0] = p["id"]
            return head + f"<h2>Contact</h2><div>Phone</div><div>{p['phone']}</div><h2>Care team</h2><div>Home location</div><div>{p['loc']}</div>"
        if tab == "/appointments":
            appts = list(p["appts"])
            if p["id"] == "lt":
                appts += [("Mar 1, 2025, 9:00 AM MST", "Office visit", "Elena Gutierrez, NP", "Front Range", "Completed")]
            rec = "".join(f'<div><a href="/patients/{p["id"]}/recurring/{r["id"]}">{r["rule"]}</a><div>{r["type"]} with {r["provider"]} at {r["location"]}</div></div>' for r in p["rec"])
            rows = "".join(f"<tr><td>{a[0]}</td><td>{a[1]}</td><td>{a[2]}</td><td>{a[3]}</td><td><span>{a[4]}</span></td></tr>" for a in appts)
            tbl = (f"<table><thead><tr><th>Date and time</th><th>Type</th><th>Provider</th><th>Location</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>"
                   if appts else "<p>No appointments</p>")
            return head + (f"<h2>Recurring appointments</h2>{rec}" if rec else "") + "<h2>Appointments, newest first</h2>" + tbl
        if tab == "/recurring":
            r = next(x for x in p["rec"] if x["id"] == m.group(3)[1:])
            ch = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in r["changed"])
            al = "".join(f"<tr><td>{a}</td><td><span>{b}</span></td></tr>" for a, b in r["dates"])
            return head + (f"<h2>Recurring appointment: {r['rule']}</h2><div>Repeats</div><div>{r['rule']}</div><div>Type</div><div>{r['type']}</div>"
                           f"<div>Provider</div><div>{r['provider']}</div><div>Location</div><div>{r['location']}</div>"
                           f"<h3>Changed dates</h3><table><thead><tr><th>Date</th><th>Change</th><th>Recorded</th></tr></thead><tbody>{ch}</tbody></table>"
                           f"<h3>All dates</h3><table><thead><tr><th>Date and time</th><th>Status</th></tr></thead><tbody>{al}</tbody></table>")
        if tab == "/documents":
            rows = "".join(f'<tr><td>{d["received"]}</td><td>{d["type"]}</td><td>{d["title"]}</td><td><a href="/documents/{d["id"]}">View</a></td></tr>' for d in p["docs"])
            return head + (f"<table><thead><tr><th>Received</th><th>Type</th><th>Title</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table>" if rows else "<p>No documents in this chart</p>")
        return head + "<p>Activity on this record</p>"
    m = re.match(r"^/documents(?:/(\w+))?$", u.path)
    if m:
        docs = [(p, d) for p in P.values() for d in p["docs"]]
        rows = "".join(f'<tr><td>{d["received"]}</td><td>{d["type"]}</td><td>{d["title"]}</td><td>{p["last"]}, {p["first"]}</td><td><a href="/documents/{d["id"]}">View</a></td></tr>' for p, d in docs)
        viewer = ""
        if m.group(1):
            d = next(d for _, d in docs if d["id"] == m.group(1))
            viewer = f'<h2>{d["title"]}</h2><button>Close document</button><iframe data-doc="{d["id"]}" src="/v/{random.getrandbits(32):x}/" style="width:900px;height:700px"></iframe>'
        return "<h1>Documents</h1>" + viewer + f"<table><thead><tr><th>Received</th><th>Type</th><th>Title</th><th>Patient</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table>"
    m = re.match(r"^/docview/(\w+)$", u.path)
    if m:
        return (f'<a href="/files/{m.group(1)}.pdf">Open in new tab</a> <a href="/files/{m.group(1)}.pdf" download="letter.pdf">Download</a>'
                f'<img src="/files/{m.group(1)}.png" alt="">')
    if u.path == "/schedule":
        return "<h1>Schedule</h1>" + "".join(f'<a href="/schedule/providers/{pid}?date=2026-10-14">{n}</a> ' for pid, n, _, a in PROVIDERS if a)
    m = re.match(r"^/schedule/providers/(\w+)$", u.path)
    if m:
        pr = next((x for x in PROVIDERS if x[0] == m.group(1)), None)
        if not pr:
            return "This page is not available"
        act = "".join(f'<option value="{x[0]}">{x[1]}</option>' for x in PROVIDERS if x[3])
        ina = "".join(f'<option value="{x[0]}">{x[1]}</option>' for x in PROVIDERS if not x[3])
        return (f"<h1>Schedule</h1><h2>{pr[1]}</h2><p>{pr[2]} · Wednesday, October 14, 2026</p>"
                f'<label>Provider</label><select name="provider"><optgroup label="Active">{act}</optgroup><optgroup label="Inactive">{ina}</optgroup></select>'
                "<p>Nothing scheduled on this day</p>")
    return "<p>This page is not available</p><p>The address may be mistyped, or the record was removed.</p>"


LOGIN_FORM = ('<html><body><form method="post" action="/login"><input type="text" name="u">'
              '<input type="password" name="pw"><button type="submit">Sign in</button></form></body></html>')
# MOCK_LOGIN=two-step: the user name page first; POST /login/identify answers with the password form.
LOGIN_STEP1 = ('<html><body><form method="post" action="/login/identify"><input type="email" name="email">'
               '<button type="submit">Next</button></form></body></html>')


class Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/login"):
            return self._send(200, LOGIN_STEP1 if os.environ.get("MOCK_LOGIN") == "two-step" else LOGIN_FORM)
        s = sess(self)
        if not s:
            return self._send(302, "", headers={"Location": "/login"})
        if self.path.startswith("/v/"):
            return self._send(200, FRAME)
        if self.path.startswith("/data"):
            p = unquote(parse_qs(urlparse(self.path).query)["p"][0])
            return self._send(200, content(s, p))
        m = re.match(r"^/files/(\w+)\.(pdf|png)$", self.path)
        if m:
            d = next(d for p in P.values() for d in p["docs"] if d["id"] == m.group(1))
            if m.group(2) == "pdf":
                return self._send(200, make_pdf(d["body"]), "application/pdf")
            return self._send(404, "")
        return self._send(200, shell(s))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if self.path.startswith("/login/identify"):
            return self._send(200, LOGIN_FORM)
        if self.path.startswith("/login"):
            time.sleep(float(os.environ.get("MOCK_SLOW_LOGIN") or 0))
            if os.environ.get("MOCK_BREAK_LOGIN"):   # sign-in never works: the form again, no session
                return self._send(200, LOGIN_FORM)
            sid = "%x" % random.getrandbits(40)
            SESS[sid] = {"loc": "Northgate", "size": 5, "page": {}}
            return self._send(302, "", headers={"Location": "/", "Set-Cookie": f"sid={sid}; Path=/"})
        s = sess(self)
        if not s:
            return self._send(403, "")
        if self.path.startswith("/setloc"):
            if os.environ.get("MOCK_BREAK_LOC"):
                return self._send(503, "")
            s["loc"] = q["loc"]
        elif self.path.startswith("/setsize"):
            s["size"] = int(q["size"])
            s["page"] = {}
        elif self.path.startswith("/setpage"):
            s["page"][q["key"]] = int(q["page"])
        return self._send(200, "ok")


MSG1 = open(__file__.replace("mock_wrenfield.py", "../practice_items.json")).read()
MSG1 = json.loads(MSG1)[0]["text"]
ITEMS = [
    {"id": "i001", "kind": "message", "text": MSG1},
    {"id": "i002", "kind": "task", "text": "Is Judith Jackson (born 2004-11-12) new or established for Kenneth Sullivan, NP on 2028-07-20?", "expected": "ESTABLISHED:2025-07-21"},
    {"id": "i003", "kind": "task", "text": "What phone number do we have on file for patient Carol Jones (born 1982-09-07)?", "expected": "(785) 310-6041"},
    {"id": "i004", "kind": "message", "text": "The vending machine by the back entrance takes cards again, but the coin slot is still jammed."},
    {"id": "i005", "kind": "task", "text": "On 2026-12-25 there is a visit with Douglas Hughes, MD for Henry, Peter, DOB 1975-06-23, and I need new vs. established for it.", "expected": "ESTABLISHED:2026-01-05"},
    {"id": "i006", "kind": "task", "text": "Referral letter received on 2025-09-16 for patient Gregory Porter (born 1976-04-08): what reason does it give for the referral?", "expected": "Chronic cough lasting more than eight weeks"},
    {"id": "i007", "kind": "task", "text": "Is Laura Torres (born 2001-02-20) new or established for Elena Gutierrez, NP on 2025-12-30?", "expected": "ESTABLISHED:2025-08-14"},
    {"id": "i008", "kind": "task", "text": "How many patients with the last name Jones can you see?", "expected": "x"},
]
QSTATE = {"i": 0, "log": [], "t0": None}


def elapsed():
    return time.time() - QSTATE["t0"] if QSTATE["t0"] else 0.0


def queue_down():
    """MOCK_QUEUE_ERRORS=a-b: the queue answers 500 while the shift clock reads between a and b seconds."""
    w = os.environ.get("MOCK_QUEUE_ERRORS")
    if not w or not QSTATE["t0"]:
        return False
    a, b = (float(x) for x in w.split("-"))
    return a <= elapsed() <= b


class Q(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def state(self):
        i = QSTATE["i"]
        shift = os.environ.get("MOCK_SHIFT")
        if shift:   # a timed shift: items cycle with fresh ids until the clock (started by the first call) runs out
            if QSTATE["t0"] is None:
                QSTATE["t0"] = time.time()
            left = float(shift) - elapsed()
            if left <= 0:
                return {"status": "ended", "reason": "time"}
            it = ITEMS[i % len(ITEMS)]
            return {"status": "open", "seconds_left": round(left, 1),
                    "item": {"id": f"{it['id']}-{i}", "kind": it["kind"], "text": it["text"]}}
        if i >= len(ITEMS):
            return {"status": "ended", "reason": "queue_finished"}
        return {"status": "open", "seconds_left": 600.0, "item": {k: ITEMS[i][k] for k in ("id", "kind", "text")}}

    def do_GET(self):
        if self.path == "/item":
            if queue_down():
                return self._json(500, {"error": {"code": "internal"}})
            return self._json(200, self.state())
        if self.path == "/calls":
            return self._json(200, CALLS)
        if self.path == "/log":
            return self._json(200, QSTATE["log"])
        self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if queue_down():
            return self._json(500, {"error": {"code": "internal"}})
        st = self.state()
        if st["status"] != "open":
            return self._json(409, {"error": {"code": "ended"}, "state": st})
        if body.get("id") != st["item"]["id"]:
            return self._json(409, {"error": {"code": "not_current"}, "state": st})
        it = ITEMS[QSTATE["i"] % len(ITEMS)]
        QSTATE["log"].append({**body, "expected": it.get("expected"), "t": round(elapsed(), 1)})
        QSTATE["i"] += 1
        self._json(200, self.state())


class L(BaseHTTPRequestHandler):
    """Scripted model that uses the agent's tools the way a good model should."""
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        time.sleep(float(os.environ.get("MOCK_SLOW_MODELS") or 0))
        self._json(200, {"data": [{"id": "fake"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        msgs = req["messages"]
        CALLS.append(msgs[0]["content"][:60] if isinstance(msgs[0]["content"], str) else "vision")

        def say(obj):
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": json.dumps(obj)}}],
                                    "usage": {"cost": 0.0005}})

        first = msgs[0]["content"]
        if not isinstance(first, str):   # screenshot of a chart: "read" the phone of the chart shown last
            return say({"phone": P[LAST_CHART[0]]["phone"], "email": "", "address": ""})
        if first.startswith("The clinic messages below"):
            excl = re.search(r"never make a patient established: ([^.]+)\.", first).group(1).split(", ")
            return say({"years": 3, "counting_statuses": ["Completed"], "excluded_services": excl,
                        "same_specialty_counts": True, "all_locations": True, "window": "standard",
                        "new_answer": "NEW", "established_example": "ESTABLISHED:2025-03-11", "other_conditions": []})
        if first.startswith("Answer the question using ONLY the document"):
            doc = first.split("DOCUMENT (", 1)[1]
            m = re.search(r"Reason for referral: ([^\n]+)", doc) if "reason" in first.split("QUESTION:")[1].lower() else \
                re.search(r"\):\n([^\n]+)", doc)
            return say({"answer": m.group(1).strip() if m else None, "confidence": 0.95})
        user = next(m["content"] for m in msgs if m["role"] == "user")
        task = user.split("\n\n")[0]
        tools_done = [m for m in msgs if m["role"] == "tool"]
        last = tools_done[-1]["content"] if tools_done else ""
        n = len(tools_done)

        def call(name, args):
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{len(msgs)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}],
                "usage": {"cost": 0.001, "prompt_tokens": 1, "completion_tokens": 1}})

        paths = re.findall(r"-> (/patients/\w+)", user)
        if "established" in task:
            m = re.search(r"(\d{4}-\d{2}-\d{2})\??$|on (\d{4}-\d{2}-\d{2})", task)
            visit = re.findall(r"\d{4}-\d{2}-\d{2}", task)
            dob = re.search(r"(?:born|DOB) (\d{4}-\d{2}-\d{2})", task).group(1)
            visit = [v for v in visit if v != dob][0]
            prov = re.search(r"((?:[A-Z][a-z]+ ){1,2}[A-Z][a-z]+, (?:MD|NP|PA|DO))", task).group(1)
            if n == 0:
                excl = re.search(r"never make a patient established: ([^.]+)\.", msgs[0]["content"]).group(1).split(", ")
                return call("new_or_established", {"patient_paths": paths, "provider": prov, "visit_date": visit,
                                                   "excluded_services": excl, "years": 3, "counting_statuses": ["Completed"]})
            res = json.loads(last)
            if res.get("safe_to_answer"):
                return call("submit_answer", {"answer": res["result"], "confidence": 0.95})
            return call("skip_task", {"reason": "unsafe: " + "; ".join(res.get("warnings", []))})
        if "phone" in task:
            if n == 0:
                return call("open_pages", {"paths": paths[:1]})
            ph = re.search(r"Phone\s*\n\s*(\(\d{3}\) \d{3}-\d{4})", last)
            return call("submit_answer", {"answer": ph.group(1), "confidence": 0.95}) if ph else call("skip_task", {"reason": "no phone"})
        if "Referral letter" in task:
            if n == 0:
                return call("open_pages", {"paths": [paths[0] + "/documents"]})
            if n == 1:
                d = re.search(r"-> (/documents/\w+)", last)
                return call("read_document", {"path": d.group(1)})
            m = re.search(r"Reason for referral: ([^\n]+)", json.loads(last).get("text", ""))
            return call("submit_answer", {"answer": m.group(1).strip(), "confidence": 0.9}) if m else call("skip_task", {"reason": "no reason"})
        if "Jones" in task:
            if n == 0:
                return call("open_pages", {"paths": ["/patients?lastName=Jones"]})
            rows = len(re.findall(r"^Jones, ", last, re.M))
            return call("skip_task", {"reason": f"saw {rows} Jones rows; warning={'WARNING' in last}"})
        return call("skip_task", {"reason": "unknown"})


def serve(port, h):
    s = ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


if __name__ == "__main__":
    serve(8811, Site), serve(8812, Q), serve(8813, L)
    print("mock wrenfield up", flush=True)
    while True:
        time.sleep(1)
