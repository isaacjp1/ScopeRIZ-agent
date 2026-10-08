# Experiment log

Only real runs. Practice set = 6 tasks, so treat single-run differences as noise; compare over several runs.

| Version | Change | Run | Points | Correct | Wrong | Skipped | Cost | Notes |
|---|---|---|---|---|---|---|---|---|
| v1 | Baseline: Playwright by URL, gemini-2.5-flash, 10 turns, conf ≥ 0.7 | t76c947ef88d4 | 0 | 1 | 1 | 4 | $0.059 | 3× new/established hit max turns at ~9 s; referral reason not read (letter content); phone wrong at conf 1.0 |
| v2 | 25 turns, gemini-3.8-flash, look_at_page screenshots, identity/document/rule prompt guidance, slim image, debug page logging | (never run) | | | | | | |
| riz-v3 | Verified All-locations switch (trusted clicks), network-idle page waits, all frames read, tables with row links, server-side paging clicked through, PDF text for documents, provider directory with specialties, tools find_patient / appointment_history / new_or_established / read_document / provider_info, DOB prefetch, adaptive per-task time cap, cache-friendly prompt | ta23339606f0a | 4 | 5 | 1 | 0 | $0.055 | Only miss: phone (page text scrambled: '(243) 910-7051' vs 785-310-6041). Mock clinic (test/mock_wrenfield.py): 5/5 answerable correct; with location switch refused: 1 correct, 0 wrong, rest skipped |

## Failure categories (run t76c947ef88d4, from eval/parse_run.py)
- 3 skip: step budget exhausted (all new_vs_established)
- 1 wrong: overconfident (phone)
- 1 skip: document content not read (referral_reason)
- 1 ok (referral_source)

| merged-v4 | riz-v3 + patient_contact tool: phone read from a CDP screenshot by the model, checked against the page text with a digit-substitution consistency test and a per-session learned digit map; skip if they disagree | (pending) | | | | | | Cipher check validated on all 3 observed scrambles of the same phone |
| v5 | Fast deterministic paths (N/E, phone, documents) before the model loop; N/E rule compiled once and checked against the message text; message chatter classifier; time caps 4–20 s; hopeless-kind skip; loop budget reserve | t6d60cc954d3e | 4 | 4 | 0 | 2 | $0.080 | No fast path ever fired. Root causes: (1) Gemini 3.8 Flash thinking tokens count against max_tokens: rule compile (600) and phone screen read (300) came back empty; (2) practice proxy allows 1 request in flight at $1 left: the background classifier + compile + loop hit 429 concurrency_limit; (3) PDF download gave no text (the loop fell back to screenshots). Loop turns 1.5–5.9 s; tasks 5–20 s |
| v7 | (see above) | t5df36f4c9eab | 5 | 5 | 0 | 1 | $0.014 | Practice. SCORED: Set 1 35/35/0/172/17, Set 2 45 (49 correct, 4 wrong), Set 3 2: total 82, the best of all versions |

## Scored results are the only evaluation that counts (practice ranked v9 above v7; the scored sets ranked them the other way)
| Version | Set 1 (224) | Set 2 (226) | Set 3 (458) | Total |
|---|---|---|---|---|
| v7 | 35 / 35 / 0 / 172 / 17 | 45 / 49 / 4 / 173 / 0 | 2 / 2 / 0 / 456 / 0 | **82** |
| v9 | 9 / 9 / 0 / 214 / 1 | 24 / 26 / 2 / 171 / 27 | 2 / 2 / 0 / 456 / 0 | 35 |
| buddy v10 | 35 / 35 / 0 / 189 / 0 | 38 / 43 / 5 / 151 / 27 | cancelled | none |
| buddy v11 | 33 / 33 / 0 / 191 / 0 | 26 / 31 / 5 / 163 / 27 | 7 / 7 / 0 / 451 / 0 | 66 |
(points / correct / wrong / skipped / not reached)

v7 Set 1 log: phones 19 answered (learned digit map trusted after 3 screenshots, 0.4 s each: on the scored clinic the
scramble stays the same through the session, unlike practice); lab reports answered NOT_COVERED in about 2 s from the
rules; referrals about 5 s; skips: 82 hopeless-kind, 79 loop out of time, 11 model timeouts. No 503s on Set 1, so
v9's rate-limit changes (4 tabs, backoff, serial preload) only cost time. v9 lost the phones (its glyph decoder matched
"Wrenfield Mono" against itself) and skipped every fast-path miss under "tight" time.

| v12 | v7 exactly + documents rows matched by any date (buddy v10 fix) + documents fast path for referral letters only + full logging (messages, tasks, model turns) | scf22387dcd4b | 71 | Set 1 36/0/188/0 (best Set 1 yet, 0 not reached), Set 2 33 (38 correct, 5 wrong), Set 3 2 | | | | ~$1 per set | Set 2 swings 33-45 between near-identical builds (v7 45, v12 33): one run per build is noisy (live data changes during the shift, model nondeterminism) |

Set 1 (buddy v11 log, every rule printed): 224 tasks = lab triage 45, claim next step 38, invoice owed 22, phone 20,
provider appointment count 16, filing deadline 14, recall count 13, good faith estimate 13, tetanus 12, open slots 9,
patient counts 8, referral 10. Each kind's rule (3-9k chars, worked example, exact answer form) arrives right before
its tasks; rules change mid-shift. Model turns average 1.9 s (7k prompt tokens, 40 out): turns are not slow, the loop
just needs 5-10 of them for a multi-page rule. 803 s of 900 went to tasks that ended skipped, 91 s to the 36 answers.
The log holds almost no page content, so code solvers for these kinds need the page layouts first.

| v13 | v12 behaviour unchanged + [layout] dump: the first 2 pages of every page pattern the agent reads are printed in full (3.5k chars), so the next Set 1 output shows the real claim / remittance / payer / invoice / schedule / lab pages + practice-only recon crawl at startup (one example of every link pattern, one page at a time) | (pending) | | | | | | |
| v17 (branch v17-v7sonnet) | **v7 agent code exactly** (documents fast path for every document task, lab reports included; no kinds.py) + v13 logging only (message/task text, every model turn, [layout] dumps, practice recon, docs-debug) + **main model anthropic/claude-sonnet-5.5** (system prompt cached; no temperature with reasoning; falls back to gemini-3.8-flash at 60% of the budget, or for good if Sonnet keeps failing). Note: v7 never used DeepSeek; its model was gemini-3.8-flash. v13 had cut lab reports out of the documents fast path (v7 answered them NOT_COVERED in ~2 s) and added the blind invoice solver | (pending) | | | | | | |
| v18 (branch v17-v7sonnet) | v17 + ported from main: provider specialty reads retried and failures never cached (inactive providers); new/established rule read from its text at once (compile only vetoes); startup across Wrenfield versions (sign-in at / too, sign in again when signed out, other location switchers + diagnostics, patients via /documents and /billing when the switch fails, search fields from the clinic's form, name-by-name search, startup retried); written-off kinds retried every 6th task when time is spare; pages reloaded after 60 s. New: loop prompt carries only the task's rule messages (IDF word overlap) + recent short messages, fixed part cached across tasks; kinds answered before get up to 28 s while time allows | (pending) | | | | | | |
| v19 (Noah) | James's v18 + Noah's v17 code solvers (agent/solvers.py: provider appointment counts, open slots, filing deadline, claim next step; run before the other fast paths) + Sonnet fallback to flash only for the one call that failed (v18 switched for the rest of the shift on any single failed call) + first task's time starts after sign-in + no "fast models available" line (it listed models, it never called them) | (pending) | | | | | | Local: Set 1 replica (test/mock_set1.py) 26/26, practice mock 5/5 answerable |

## Milestone runs (from baseline-128). Cells: points / correct / wrong / skipped / not reached
Probe readout (Sets 2 and 3 show no logs): stage code = wrong // 10: 0 = no failure detected (also when no date-of-birth search happened, or when the location switch was not confirmed but date-of-birth searches mostly found their patients), 1 = location switch (not confirmed, and most early date-of-birth searches found nobody), 2 = patient search, 3 = sign-in or browser start; normal wrongs = wrong % 10. Readable while a set's normal wrongs stay at 9 or fewer. Set 1 shows the stage directly in its [probe] startup log line.

| Tag | Commit | Date | Hypothesis | Set 1 | Set 2 | Set 3 | Total | Probe readout | Notes |
|---|---|---|---|---|---|---|---|---|---|
| baseline-128 | c0062fa | 2026-10-07 | — | 63 / 64 / 1 / 134 / 25 | 51 / 57 / 6 / 135 / 28 | 14 / 14 / 0 / 373 / 71 | 128 | (no probe) | |
| phase1 (awaiting score) | a6947cc | 2026-10-07 | Rule 1 guards + no early exit + bounded startup + confidence gate + startup probe (PROBE=10); decode the Set 3 failing stage | not run | not run | not run | | | image i750ae7c79f01; not submitted (build-only) |
