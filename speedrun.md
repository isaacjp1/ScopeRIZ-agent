
# Speedrun: getting started

## Start here

Build an agent that answers a clinic's back-office requests using its records system. Your goal is to answer more tasks correctly within each set's length and model budget.

Start by [exploring the practice clinic](https://practice.speedrun.scopehealth.com/). Then run the reference agent, inspect its practice results, improve it and submit your image.

Follow your runs and submissions in [Judge](https://speedrun.scopehealth.com/#judge). Use **Test** for practice and **Submit** for scoring.

The [Rules](https://speedrun.scopehealth.com/#rules) are a separate document.

## Why this problem

An AI agent can use a web application by clicking and typing, like a person. It takes several times as many steps as a person for the same task, and it does not get faster with repetition.

Scope builds agents for clinic back offices, where staff answer the same kinds of requests from the same records system all day. In Speedrun you build an agent that works through a queue of those requests, answers them correctly, and gets faster as it goes.

The competition starts Monday, Oct 5 at 10:00 PM Eastern and ends midday Friday, Oct 9. The leaderboard stops updating about a day before the end.

## The product

The clinic keeps its records in **Wrenfield**, a web product with patients, charts, a schedule, billing, documents, reports and more. Your agent does all of its work in it. Different clinics may have different versions of Wrenfield.

![The Wrenfield dashboard: a sidebar with Patients, Schedule, Inbox, Billing, Documents and other pages, and cards for today's appointments, open slots, new patients, documents and invoices.](https://speedrun.scopehealth.com/guide/wrenfield-dashboard.webp)

*The Wrenfield dashboard.*

During a run, the agent gets Wrenfield's address and its own sign-in as environment variables.

[Explore the practice clinic](https://practice.speedrun.scopehealth.com/). You can browse its patients, schedule, billing and documents before installing the kit. All records are synthetic. The organizers may reset its data.

The [Judge screen](https://speedrun.scopehealth.com/#judge) also links to this clinic in Test. `arena sets` prints its current address.

## The setup

You build an agent and package it as a Docker image. The agent works one shift in the back office of a clinic, inside Wrenfield.

The shift's work is a **set**: a fixed queue of items at one clinic. Every item is plain text, and each item is one of two kinds:

- A **message** tells the agent something. It may say how a kind of work is done, change an earlier rule, state a fact, or have nothing to do with the work. The agent acknowledges it. Later tasks may depend on it, so the agent has to keep it.
- A **task** asks for one answer. The agent answers it or skips it.

The agent holds one item at a time. It gets the next item only after it closes the current one. It cannot look ahead in the queue, and it cannot reopen an item it has closed.

Diagram: Four items in a queue, taken in order: a task, a message, a task and a task. Each task is answered or skipped. The message is acknowledged and can change the answer to a later task. The agent gets item 2 only after it closes item 1.

### An example from the practice set

These are the first four items of the practice set, in order, with their full text. Each shows what the queue gives, the work an agent does in Wrenfield, and the request that closes the item. The first item is a message that sets a rule; it is long, as rules often are, and the tasks after it depend on it.

#### Item 1, message

> **From billing: telling new patients from established ones**
>
> **New or established?**
>
> The two are billed with different visit codes, so the front desk and the coders often ask which one a visit is. Each question is about one visit: a patient, a provider, and a date. The visit may be coming up or may already have happened.
>
> A patient is established if they had an appointment that counts with the same provider, or with another provider in the same specialty, in the 3 years before the visit date. Otherwise they are new.
>
> **Which past appointments count**
>
> An appointment counts only if all of these are true:
>
> - Its status is Completed. Scheduled, Cancelled, Late cancel, and No-show appointments do not count.
- It was with the same provider or with a provider in the same specialty. Each provider has one specialty. Providers who have left the group keep the specialty they had.
- It is not one of these services, which never make a patient established: Lab draw, Immunization, X-ray, Ultrasound, Electrocardiogram, Echocardiogram, Allergy injection, Blood pressure check, Hearing test, Vision screening, Infusion therapy, Anticoagulation check.
- Its date is inside the 3-year window.
>
> Look at all of the patient's appointments: those at every one of our locations, those filed under a duplicate record that was later merged into the patient's chart, and each date of a recurring appointment, with the status that date has. The date of an appointment is the calendar date at the location where it took place.
>
> **The 3-year window**
>
> The window starts the day after the same date 3 years earlier and ends the day before the visit. For a visit on 2026-11-05 the window is 2023-11-06 through 2026-11-04, so an appointment on 2023-11-05 is too old. Appointments on the visit date or after it do not count. For a visit on February 29, the same date 3 years earlier is February 28.
>
> **How to answer**
>
> If nothing counts, answer `NEW`. Otherwise answer `ESTABLISHED:` followed by the date of the most recent appointment that counts, as YYYY-MM-DD, for example `ESTABLISHED:2025-03-11`. If a later appointment in the specialty does not count, such as a no-show or a lab draw, ignore it and give the date of the last one that does.
>
> **Example**
>
> If someone asks: "Is Jack Williams a new patient or established for the visit with Logan Brown, MD on 2026-10-29?"
>
> The answer is `ESTABLISHED:2026-03-31`.
>
> Logan Brown, MD is in Internal Medicine. For a visit on 2026-10-29 the window is 2023-10-30 through 2026-10-28. The patient has 1 appointment in all, 1 with a provider in Internal Medicine. The most recent one that counts inside the window is the Follow-up visit on 2026-03-31 with Frances Ward, NP.

The work

1. Keep the text for the rest of the shift. Items 2 and later may use its rule.

What the agent sends

```
POST /item/close
{ "id": "i001",
  "acknowledge": true }
```

Messages score nothing. The response is the next item.

#### Item 2, task

> Billing question: Guadalupe Driscoll, visit with Sharon Morales, MD on 2025-11-05. Is that a new or an established patient visit?

The work

1. Set the location at the top to All locations, open Patients, and search for the last name Driscoll.

2. Open Guadalupe Driscoll's chart and its Appointments tab.

3. All four appointments are after 2025-11-05, so none is in the 3-year window before the visit. By the rule in item 1, the answer is `NEW`.

![Guadalupe Driscoll's Appointments tab in Wrenfield: four completed appointments dated Nov 10, 2025, Jan 20, 2026, Feb 5, 2026 and Sep 23, 2026.](https://speedrun.scopehealth.com/guide/wrenfield-appointments.webp)

*The Appointments tab of Guadalupe Driscoll's chart.*

What the agent sends

```
POST /item/close
{ "id": "i002",
  "answer": "NEW" }
```

Correct: +1.

#### Item 3, task

> Referral letter received on 2025-11-10 for patient Randy Singh (born 1939-12-04): which practice made the referral?

The work

1. Search Patients for Singh and open the Randy Singh born 12/04/1939.

2. Open the Documents tab. The referral letter received Nov 10, 2025 is from Plains Dermatology Associates.

![Randy Singh's Documents tab in Wrenfield: one document, titled Referral letter from Plains Dermatology Associates, received Nov 10, 2025.](https://speedrun.scopehealth.com/guide/wrenfield-referral.webp)

*The Documents tab of Randy Singh's chart.*

What the agent sends

```
POST /item/close
{ "id": "i003",
  "answer": "Plains Dermatology Associates" }
```

Correct: +1.

#### Item 4, task

> What phone number do we have on file for patient Julie Martínez (born 2010-01-10)?

The work

1. Search Patients for Martinez and open the Julie Martínez born 01/10/2010.

2. Read the phone number under Contact on the Demographics tab.

![Julie Martínez's chart in Wrenfield, Demographics tab: phone (620) 323-6931, no email, an address in Harbor Glen, KS.](https://speedrun.scopehealth.com/guide/wrenfield-phone.webp)

*The Demographics tab of Julie Martínez's chart.*

What the agent sends

```
POST /item/close
{ "id": "i004",
  "answer": "(620) 323-6931" }
```

Correct: +1. The answer is compared as a phone number, so `"6203236931"` would also be correct.

The [Agent interface](https://speedrun.scopehealth.com/#interface) document lists how each kind of answer is compared.

## Using the Judge

[Open the Judge screen](https://speedrun.scopehealth.com/#judge) to start practice runs, submit your agent, and track progress and results. You can also select Judge from the dock.

- **Test:** Choose an uploaded image and start a practice run. Open a run to follow its live output, answers, outcomes, and expected answers. The practice clinic link is here too.
- **Submit:** Choose an uploaded image and submit it for scoring. Open a submission to track each scored set and read its points and task counts.

Upload images with `arena push` in your terminal. The Judge screen and CLI use the same account and show the same work.

On a phone, open the Judge link on a computer to use its controls.

## Testing your agent

You can test your agent in two places before you submit: on your own machine, and on the practice set. Nothing in this section counts toward the ranking.

### On your own machine

On your own machine you write your agent and build its image. From your agent's directory:

The judge requires Linux AMD64 images (`linux/amd64`). On Apple Silicon Macs, Docker defaults to ARM64. Always include `--platform linux/amd64` when you build. The CLI rejects other platforms before upload.

```
docker build --platform linux/amd64 -t my-agent .
```

Or build the reference agent from the kit. This build downloads Node packages, OpenCode and a browser, so it needs internet access:

```
docker build --platform linux/amd64 -t my-agent reference-agent
```

You cannot run a clinic or a set on your machine. The practice clinic is hosted by the organizers, and a set's queue exists only on the judge. What you can run on your machine is your agent's code against the hosted practice clinic, with your development key.

### Your development key

The [Set up](https://speedrun.scopehealth.com/#setup) window gives you one OpenRouter key with a $20 limit. It is for running your agent's code on your own machine while you write it. Runs on the judge never use it: each run there gets a key of its own with the set's model budget. If you need more than $20, ask an organizer.

Use it the way your agent uses `LLM_API_KEY` on the judge. The judge's model endpoint is an OpenAI-compatible proxy to OpenRouter with the same routes, so code written against OpenRouter works on the judge once it reads the address and the key from the environment. To run your code against the practice clinic from your machine, set:

| Variable | On your machine |
| --- | --- |
| `SITE_URL` | `https://practice.speedrun.scopehealth.com`. It has no sign-in: `SITE_USERNAME` and `SITE_PASSWORD` can be anything, and its sign-in page sends every visitor straight in. |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` |
| `LLM_API_KEY` | Your development key |
| `QUEUE_URL` | Nothing. There is no queue on your machine. Give your code a task's text directly: the text of every practice task and its expected answer are in the record of a practice run. |

Do not put the key into your image. The image is uploaded to the judge, and the judge gives your agent a key of its own.

### The practice set

A run is one execution of your image on one set. A practice run is one you start yourself: your image, on the practice set, on the judge. Scored runs happen only as part of a submission.

|  | Practice set |
| --- | --- |
| Tasks | 6 |
| Length | 20 min |
| Model budget, paid by Scope | $1 |
| Allowance | 30 runs a day, 2 at once |
| What you see | Every item, your answer, the expected answer, and your agent's output as it runs |

Every set has a fixed length and a model budget. When the budget is used up, the model endpoint refuses further requests and the run goes on. A run that fails on the organizers' side does not count against an allowance.

Run it with your local image. `--agent` uploads the image first. The command prints your agent's output as the run goes, its own lines with the prefix `[arena]`, and the record of the run at the end. It takes up to 20 minutes.

```
arena test --set practice --agent my-agent
```

List your runs:

```
arena runs
```

Print one run. For each item you will see its text, what your agent sent, the outcome and the expected answer. Open the run under **Test** in the [Judge screen](https://speedrun.scopehealth.com/#judge) to see the same results and live output.

```
arena runs <run id>
```

[Open the practice clinic](https://practice.speedrun.scopehealth.com/) to inspect the records behind a task. The [Judge screen](https://speedrun.scopehealth.com/#judge) also links to it in Test. This command prints its current address:

```
arena sets
```

For example, a team starts a practice run after each change, compares every answer with the expected one, and opens the hosted Wrenfield to see where a wrong answer came from.

## Submitting and scoring

A submission is what counts toward the ranking. Upload your image. This prints its id.

```
arena push my-agent
```

Submit it. This runs the image once on each of the three scored sets. You have 2 submissions a day, with 1 in progress at a time. With `--wait` the command prints a line when a scored set's tally changes, such as `[arena] Scored set 1 of 3: 120 of 224 tasks closed, 81 correct, 9 wrong, 30 skipped`.

```
arena submit --image <id> --wait
```

The three scored sets are at clinics nobody has seen. On each you see the points and the counts of correct, wrong, skipped and not reached tasks, updated as the run goes.

|  | Tasks | Length | Model budget, paid by Scope | What else you see |
| --- | --- | --- | --- | --- |
| Set 1 | 224 | 15 min | $8 | Your agent's output, live and afterwards with `arena logs <run id>` |
| Set 2 | 226 | 15 min | $8 | Nothing else |
| Set 3 | 458 | 30 min | $15 | Nothing else |

Read your scores in **Submit** on the [Judge screen](https://speedrun.scopehealth.com/#judge), or use the command line:

```
arena scores
```

### Scoring

| Outcome | Points |
| --- | --- |
| Correct answer | +1 |
| Wrong answer | −1 |
| Skipped task | 0 |
| Task not reached before the run ended | 0 |
| Message | 0 |

A run's score is the sum of its tasks' points. A submission's score is the sum of its three runs.

### The leaderboard

The leaderboard has one row per team, with the score of the team's **latest finished submission**. An earlier, higher score does not count. A tie goes to the team with more time left when it finished the queue of the final set, then to the team with fewer wrong answers.

The board stops updating about a day before the end. After that, each team still sees its own later scores.

For example, team A's latest submission scored 120 on Set 1, 150 on Set 2 and 210 on Set 3. Team B scored 520 two days ago, then submitted again and got 430.

| Team | Earlier submission | Latest finished submission | On the leaderboard |
| --- | --- | --- | --- |
| A | None | 480 (120 + 150 + 210) | 480, first |
| B | 520 | 430 | 430, second |

Print the leaderboard:

```
arena leaderboard
```

### The final set

After submissions close, the organizers run each team's latest finished submission once on the final set: 924 tasks in 60 minutes with a $25 model budget, at a clinic nobody has seen. Nothing of that run is shown until the final ranking is published. The final ranking adds its points to the team's latest submission's score.

## The interface and the reference agent

The [Agent interface](https://speedrun.scopehealth.com/#interface) document has the full contract. In short:

Start registration with [Google](https://speedrun.scopehealth.com/signup/google/) or [GitHub](https://speedrun.scopehealth.com/signup/github/). Select your account and approve access in your browser. Then complete your first name, last name, and year in Register. You can also create an account with your email address in Register.

1. Read the environment variables the judge sets: `QUEUE_URL`, `SITE_URL`, `SITE_USERNAME`, `SITE_PASSWORD`, `LLM_BASE_URL`, `LLM_API_KEY`, `SHIFT_SECONDS` and `WORK_DIR`.

2. `GET $QUEUE_URL/item` returns the open item: its `id`, its `kind` and its `text`.

3. `POST $QUEUE_URL/item/close` with the item's `id` and one of `"answer"`, `"skip": true` or `"acknowledge": true`.

4. The response holds the next item. Repeat until `status` is `ended`.

The agent's container has 4 CPUs, 8 GB of memory and no internet. It reaches only the queue, Wrenfield and the model endpoint, which serves any OpenRouter model except those with web search or browsing built in. The [Agent interface](https://speedrun.scopehealth.com/#interface) document describes the sandbox.

The [Reference agent](https://speedrun.scopehealth.com/#reference) in the kit (`reference-agent/`) implements this interface. It is a Node program with a headless browser that runs one model loop per task with `anthropic/claude-sonnet-5.5` and skips a task after 150 seconds.

On Oct 5 it scored 0 on the practice set: 0 correct, 0 wrong and 6 skipped. It ran out of its 150 seconds on five tasks and skipped the sixth. It is meant as a working example of the interface.

## Getting started

First, [explore the practice clinic](https://practice.speedrun.scopehealth.com/). Look at a patient record, an appointment and an invoice.

Start registration with [Google](https://speedrun.scopehealth.com/signup/google/) or [GitHub](https://speedrun.scopehealth.com/signup/github/). Select your account and approve access in your browser. Then complete your first name, last name, and year in Register. You can also create an account with your email address in Register.

1. [Register](https://speedrun.scopehealth.com/#register) on this site, alone or as a team of up to three. In the [Set up](https://speedrun.scopehealth.com/#setup) window, show your install command and run it in a terminal while Docker is running. It downloads the kit into `speedrun-kit`, loads the `arena` command line, and signs it in. Then put the command line on your PATH:

   ```
   cd speedrun-kit
   ```

   ```
   export PATH="$PWD:$PATH"
   ```

2. [Open Judge](https://speedrun.scopehealth.com/#judge) to track runs and submissions. Test your agent with the commands in **Testing your agent**.

3. Submit it with the commands in **Submitting and scoring**.

