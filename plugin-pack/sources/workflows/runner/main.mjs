// runner/main.mjs — one Node process per workflow run (REQ-032). It pre-scans the script (REQ-003),
// runs it through the vendored pi-subagents runtime (../vendor/pi-subagents/, REQ-002), forwards
// every WorkflowHost call to the plugin over stdio, writes the journal and exits with the result.
//
// Stdio line protocol: one JSON object per line, UTF-8, both directions. stderr is diagnostics only.
//
// plugin → runner
//   first line, the run request:
//     {"op": "run", "script": str, "args"?: json, "runDir": str,
//      "limits"?: {"concurrency"?: int, "agents"?: int, "items"?: int, "nested"?: int},
//      "budget"?: int | null, "replay"?: str}
//     runDir must exist; the runner (re)creates runDir/journal.jsonl (mode 0600) and appends one
//     vendored WorkflowJournalEntry {index, key, ok, text?, resumed?} per settled agent call.
//     replay is a journal file to resume from (it may be runDir/journal.jsonl: it is read first, and
//     replaced only once the run gets past the replayed prefix or completes; see ./journal.mjs).
//     budget sets the script's budget.total (remaining() = total - spent()); absent or null leaves
//     total null. limits default to the runtime's own caps.
//   replies to a runner call, matched by id:
//     {"id": n, "ok": true, "result": <see the call>}   or   {"id": n, "ok": false, "error": str}
//     (ok false fails that call: a failed agent for spawn/resume/gate, a throwing workflow() for load)
//   controls, any time:
//     {"op": "abort"}                      stop the run (also on stdin EOF, SIGTERM and SIGINT)
//     {"op": "skip", "index": n}           skip agent n (or "agent": agentId); its agent() is null
//   a line that is not a JSON object fails the run (status "failed"); write with allow_nan=False.
//
// runner → plugin
//   calls, each awaiting one reply:
//     {"op": "spawn", "id": n, "req": {agentId, index, prompt, label, agentType, model?, effort?,
//        schema?, isolation?, phaseIndex?, phaseTitle?, gate?}}
//        model passes through unchanged (harness prefix included); schema is the script's JSON Schema
//        result: {ok, text?, error?, skipped?, tokens?, outputTokens?, toolCalls?, cwd?, gate?: {ok, output}}
//        (a schema agent's text is its JSON answer; a gate result means the host already ran the gate)
//     {"op": "resume", "id": n, "agent": agentId, "prompt": str}     result: as spawn
//     {"op": "gate", "id": n, "command": str, "agent": agentId, "cwd"?: str}   result: {ok, output}
//     {"op": "load", "id": n, "ref": {name?, scriptPath?}}
//        result: {ok: true, script} or {ok: false, message}; a loaded script is pre-scanned as well
//   notifications:
//     {"op": "abort", "agent": agentId}    stop that child (run abort, skip)
//     {"op": "progress", "entries": [...]} the vendored WorkflowEntry batch, in emission order
//     {"op": "done", "status": "completed"|"failed"|"killed"|"refused", "value"?, "error"?,
//      "line"?, "meta"?, "agentCount", "replayedCount"}   last line; "line" only on a refusal
//   exit code: 0 completed, 1 failed or refused, 2 killed.
import { createInterface } from "node:readline";
import { join } from "node:path";
import { statSync } from "node:fs";
import { runWorkflow } from "../vendor/pi-subagents/runtime.js";
import { readJournal } from "../vendor/pi-subagents/journal.js";
import { createBridge } from "./bridge.mjs";
import { createStdioHost } from "./host.mjs";
import { openJournal } from "./journal.mjs";
import { metaEnd, prescan, Refusal } from "./prescan.mjs";

const EXIT = { completed: 0, failed: 1, refused: 1, killed: 2 };
const LIMITS = { concurrency: "concurrency", agents: "agentCap", items: "itemCap", nested: "nestedCap" };

const abort = new AbortController();
let control = null;
let finished = false;
let journal = null;
let protocolError = null;
// Latest call index per agent id: a resumed call keeps its agent's id under a new index.
const indexOf = new Map();
const bridge = createBridge((line) => process.stdout.write(line), onControl, (message) => {
  protocolError ??= message;
  abort.abort();
});

function onControl(msg) {
  if (msg.op === "abort") abort.abort();
  else if (msg.op === "skip") {
    const index = Number.isInteger(msg.index) ? msg.index : indexOf.get(msg.agent);
    if (Number.isInteger(index)) control?.skip(index);
  }
}

function onProgress(batch) {
  for (const entry of batch) if (entry.type === "workflow_agent") indexOf.set(entry.agentId, entry.index);
  bridge.notify({ op: "progress", entries: batch });
}

function finish(done) {
  if (finished) return;
  finished = true;
  try {
    journal?.close(done.status);
  } catch {
    // The replayed journal stays as it was; only this run's part file may be left behind.
  }
  process.stdout.write(JSON.stringify({ op: "done", agentCount: 0, replayedCount: 0, ...done }) + "\n",
    () => process.exit(EXIT[done.status] ?? 1));
}

const positive = (value) => Number.isInteger(value) && value > 0;

/** The runtime options of a request, or a thrown Error naming the bad field. */
function runOptions(request) {
  if (typeof request.script !== "string") throw new Error("the run request has no script");
  if (typeof request.runDir !== "string" || !statSync(request.runDir, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error("the run request's runDir is not a directory");
  }
  const options = {};
  for (const [key, value] of Object.entries(request.limits ?? {})) {
    if (!(key in LIMITS) || !positive(value)) throw new Error(`limits.${key} must be a positive integer`);
    options[LIMITS[key]] = value;
  }
  if (request.budget != null && !positive(request.budget)) throw new Error("budget must be a positive integer");
  return options;
}

/** budget.total for this run, set on the meta block's closing line so no line number moves. */
function withBudget(script, total) {
  if (total == null) return script;
  const end = metaEnd(script);
  // Through globalThis: the script may declare a `budget` of its own, which would shadow the global.
  return `${script.slice(0, end)};globalThis.budget.total = ${total};`
    + "globalThis.budget.remaining = function () { return globalThis.budget.total - globalThis.budget.spent() };"
    + script.slice(end);
}

async function run(request) {
  let options;
  try {
    options = runOptions(request);
    prescan(request.script);
  } catch (error) {
    if (error instanceof Refusal) return finish({ status: "refused", error: error.message, line: error.line });
    return finish({ status: "failed", error: error.message });
  }
  const entries = typeof request.replay === "string" ? readJournal(request.replay) : [];
  try {
    journal = openJournal(join(request.runDir, "journal.jsonl"), entries, request.replay);
    const result = await runWorkflow({
      ...options,
      script: withBudget(request.script, request.budget),
      args: request.args,
      host: createStdioHost(bridge, { prescan }),
      signal: abort.signal,
      onProgress,
      onControl: (surface) => { control = surface; },
      journal: {
        entries,
        // A throw here would break the runtime's handler: fail the run through the done line instead.
        append: (entry) => {
          try {
            journal.append(entry);
          } catch (error) {
            protocolError ??= `journal write failed: ${error.message}`;
            abort.abort();
          }
        },
      },
    });
    const { status, value, error, meta, agentCount, replayedCount } = result;
    if (protocolError !== null) finish({ status: "failed", error: protocolError, agentCount, replayedCount });
    else finish({ status, value, error, meta, agentCount, replayedCount });
  } catch (error) {
    finish({ status: "failed", error: protocolError ?? (error instanceof Error ? error.message : String(error)) });
  }
}

let started = false;
createInterface({ input: process.stdin }).on("line", (line) => {
  if (started) return bridge.receive(line);
  started = true;
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    return finish({ status: "failed", error: "the first line is not a JSON run request" });
  }
  if (request?.op !== "run") return finish({ status: "failed", error: "the first line is not a run request" });
  void run(request);
}).on("close", () => (started ? abort.abort() : finish({ status: "failed", error: "no run request" })));
for (const signal of ["SIGTERM", "SIGINT"]) {
  // After finish the done line may be stuck behind a full stdout pipe: exit without waiting for it.
  process.on(signal, () => (finished ? process.exit(EXIT.killed)
    : started ? abort.abort() : finish({ status: "killed", error: `${signal} before the run` })));
}
