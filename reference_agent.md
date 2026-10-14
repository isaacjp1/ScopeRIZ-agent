
The kit includes a reference agent in `reference-agent/`. It works a shift from start to end and shows one way to use the Agent interface.

## What it is

The reference agent is a Node program. For each task it starts a new session of OpenCode, an open-source coding agent, which has a headless Chromium browser and a shell. The session calls `anthropic/claude-sonnet-5.5` through the run’s model endpoint at `LLM_BASE_URL` with `LLM_API_KEY`. A session may take at most 150 seconds, and it is stopped 4 seconds before the run’s time is up. When the session has not produced an answer by then, the agent skips the task. When fewer than 20 seconds are left for a session, the agent skips the task without starting one.

## How it handles items

The agent asks the queue for its first item as soon as it starts, without looking at the product first (`src/shift.ts`). It acknowledges each message at once and keeps the message’s text for the rest of the run.

For a task, it builds a prompt (`src/shiftPrompt.ts`) with the product’s address and sign-in, the text of every message so far in the order they arrived, the task, the seconds available, and the scoring. The prompt tells the model to answer only from data it found in the product, to answer null otherwise, and not to change any data. The model is asked to write its checked answer to a file, `answer.json`, and then to reply with the answer inside `<answer>` tags.

When the session ends, the agent takes the last answer tag in the model’s reply, or, if there is none, the content of `answer.json` (`src/answer.ts`). An empty answer or `null` becomes a skip. Text that reads as JSON (a string, a number, or a list of them) is sent as that value, and any other text is sent as it is. If the queue refuses a close with `not_current` or `ended`, the agent goes on from the state in the refusal. When the run has ended, the process exits with status 0, and with status 1 if it could not go on (`src/shiftProcess.ts`).

It keeps nothing from one task to the next except the messages. Every task signs in again and finds its way through the product again, which costs time and model spend.

## Build and run it

From the kit directory, build the image. The build needs internet access, because it installs the Node packages, OpenCode, and Chromium. The image is about 1.8 GB.

```
docker build -t my-agent reference-agent
```

Then run it on the practice set. `--agent my-agent` uploads the local image first. To use an image you have already uploaded with `arena push my-agent`, pass `--image <id>` instead.

```
arena test --set practice --agent my-agent
arena test --set practice --image <id>
```

The command prints the agent’s output as the run goes on, then the record of the run. The Getting Started guide has the other `arena` commands.

## Its score

On October 5 it scored 0 on the practice set: 0 correct, 0 wrong, and 6 skipped. It ran out of its own 150 seconds on five tasks and skipped the sixth. It is there to show the interface working, and it is slow. Set 1 has 224 tasks in 15 minutes, about 4 seconds per task, and the agent gives each task up to 150 seconds.

## What to change first

-   `src/shift.ts`, `workTask`: each task starts a new OpenCode session with a new browser, which signs in again. Keeping a signed-in browser across tasks, and keeping what the agent learned about the product, saves that time on every later task.
-   `src/config.ts`: `defaultModel` and `REFERENCE_TASK_SECONDS` set the model and the time per task. Both can also be set with `ENV` in the `Dockerfile`, as `REFERENCE_MODEL` and `REFERENCE_TASK_SECONDS`.
-   `src/shiftPrompt.ts`: the prompt of one task. What you know about the product from looking around in it can go here, or into `REFERENCE_NOTES`, which is added to the prompt of every task.

`reference-agent/README.md` lists every file. The types of the queue API are in `contracts/src/shiftProtocol.ts` as zod schemas you can copy into an agent of your own.
