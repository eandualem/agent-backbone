// OpenCode plugin: push agent state to the agent-backbone state directory.
//
// Wired at launch through OPENCODE_CONFIG_CONTENT ({"plugin": ["file://…"]}),
// which OpenCode merges on top of the user's own configuration, so nothing
// in ~/.config/opencode or the repository is touched. Verified against
// OpenCode 1.18 in its TUI: `session.status` carries {type: "busy" | "idle"},
// `session.idle` ends a turn, `permission.asked` / `permission.replied`
// bracket a permission dialog. (`opencode run` exits before plugin handlers
// finish; the backbone only starts the TUI.) With OPENCODE_EXPERIMENTAL_PLAN_MODE
// the plan agent ends a plan with `plan_exit`, which asks "Plan at <file> is
// complete. Would you like to switch to the build agent…?" (`question.asked`,
// answered by `question.replied` / `question.rejected`; 1.18.32).
//
// Writes the same <state_dir>/<agent>.json the Python hooks write. Only the
// root session counts: sessions with a parentID are OpenCode's own subagents.
// The running model is the one a completed reply names (`message.updated`,
// provider/model as `--model` takes it), never a request's: title generation
// asks the small model. The same replies feed the diagnostics log, since
// OpenCode's screen shows neither an error's status nor a model switch:
// `request_error` is the last reply's error name and HTTP status (never the
// provider's message), `model_changed` the model a reply last switched to.
// A reply the user stopped (MessageAbortedError) counts as neither.
//
// What the backbone offers a working agent (a steer, a high-priority batch;
// <state_dir>/context/, see backbone_state.py) is taken after each tool call
// and joins the running turn as a user message, OpenCode's own path for
// input typed while it works: the turn's next step reads it (verified live
// against OpenCode 1.18).
//
// OpenCode reads skills only when its instance starts. When the backbone
// changes a running agent's skill links it leaves <state_dir>/<agent>.skills-reload;
// between turns the plugin restarts the instance (client.instance.dispose(),
// verified on 1.18.35: the session and its history stay, skills are read
// again). Never during a turn: disposing aborts it.

import fs from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";

const STATE_IDLE = "idle";
const STATE_BUSY = "busy";
const STATE_WAITING = "waiting_for_human";
const REASON_PERMISSION = "permission";
const REASON_PLAN = "plan";
async function shellActions(command, cwd, phase) {
  // Reuse the shipped stdlib parser; never interpret quoted examples as commands.
  try {
    const helper = fileURLToPath(new URL("backbone_state.py", import.meta.url));
    const stdout = await new Promise((resolve, reject) => {
      const child = execFile("python3", [helper, "--shell-actions"], {
        encoding: "utf8", timeout: 15000,
      }, (error, output) => error ? reject(error) : resolve(output));
      child.stdin.on("error", reject);
      child.stdin.end(JSON.stringify({ command, cwd, phase }));
    });
    return JSON.parse(stdout);
  } catch {
    return []; // Hook failures must not stop the agent.
  }
}

// The offer protocol of backbone_state.py (take_context, retire_steers), in
// process and synchronous: no helper process can lose what was taken, and a
// turn's end is marked before any later event runs.
const launchID = () => (process.env.BACKBONE_LAUNCH_ID || "").trim();
const offerDirs = (t) => {
  const agentDir = path.join(t.dir, "context", t.agent);
  return launchID() ? [agentDir, path.join(agentDir, launchID())] : [agentDir];
};

function offers(dir, prefix = "") {
  try {
    return fs.readdirSync(dir).filter((name) => name.startsWith(prefix) && name.endsWith(".md"));
  } catch {
    return [];
  }
}

function takeContext(t, children) {
  // Batches for the agent first (queue ids, oldest first), then this session's steers.
  const key = (name) => {
    const stem = name.slice(0, -3);
    return /^\d+$/.test(stem) ? [0, Number(stem), stem] : [1, 0, stem];
  };
  const order = (a, b) => {
    const [x, y] = [key(a), key(b)];
    return x[0] - y[0] || x[1] - y[1] || (x[2] < y[2] ? -1 : x[2] > y[2] ? 1 : 0);
  };
  const texts = [];
  const directories = offerDirs(t);
  const current = readCurrent(t);
  for (const dir of directories) {
    for (const name of offers(dir).sort(order)) {
      const offer = path.join(dir, name);
      try {
        if (forEarlierTurn(offer, current, children)) {
          fs.renameSync(offer, `${offer.slice(0, -3)}.missed`);
          continue;
        }
        // Read first: once it is .taken the backbone may settle and remove it.
        const text = fs.readFileSync(offer, "utf8");
        fs.renameSync(offer, `${offer.slice(0, -3)}.taken`);
        texts.push(text);
      } catch { /* the backbone claimed it first, or it is gone */ }
    }
  }
  return texts;
}

// The session a subagent ultimately works for: its parent, or the parent's own
// parent for a nested one, up to the first session that is not a subagent.
function topOf(children, sessionID) {
  const seen = new Set();
  let id = sessionID;
  while (children.has(id) && !seen.has(id)) {
    seen.add(id);
    id = children.get(id);
  }
  return id;
}

function forEarlierTurn(offer, current, children) {
  // Match backbone_state.py: compare only identity fields present on both
  // sides, and keep legacy offers without readable turn metadata usable.
  try {
    const turn = JSON.parse(fs.readFileSync(`${offer.slice(0, -3)}.turn`, "utf8"));
    if (!turn || typeof turn !== "object" || Array.isArray(turn)) return false;
    // Offered to this prompt while the state named a subagent not known yet:
    // its parent's turn, which the state names again since.
    if (turn.prompted_at != null && turn.prompted_at === current.prompted_at
        && children.has(turn.session_id) && topOf(children, turn.session_id) === current.session_id) {
      return false;
    }
    return ["prompted_at", "session_id"].some((key) =>
      turn[key] != null && current[key] != null && turn[key] !== current[key]);
  } catch {
    return false;
  }
}

function retireSteers(t) {
  // The turn ended: steers still offered were for it, never for the next task.
  if (!launchID()) return;
  const dir = offerDirs(t)[1];
  for (const name of offers(dir, "steer-")) {
    const offer = path.join(dir, name);
    try {
      fs.renameSync(offer, `${offer.slice(0, -3)}.missed`);
    } catch { /* taken or settled meanwhile */ }
  }
}

function target() {
  const agent = (process.env.BACKBONE_AGENT || "").trim();
  const dir = (process.env.BACKBONE_STATE_DIR || "").trim();
  if (!agent || !dir) return null;
  return { agent, dir };
}

function launchStatePath(t) {
  if (!launchID()) return null;
  const identity = createHash("sha256").update(launchID()).digest("hex");
  return path.join(t.dir, "launches", t.agent, `${identity}.json`);
}

function readCurrent(t) {
  try {
    const scoped = launchStatePath(t);
    const file = scoped && fs.existsSync(scoped) ? scoped : path.join(t.dir, `${t.agent}.json`);
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return {};
  }
}

function writeState(t, record) {
  if (launchID()) record.launch_id = launchID();
  fs.mkdirSync(t.dir, { recursive: true });
  const files = [launchStatePath(t), path.join(t.dir, `${t.agent}.json`)];
  for (const file of files.filter(Boolean)) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(record));
    fs.renameSync(tmp, file);
  }
  if (record.session_id) {
    try {
      const launch = process.env.BACKBONE_LAUNCH_ID || null;
      const identity = createHash("sha256").update(
        JSON.stringify([t.agent, record.runtime, record.session_id, launch]),
      ).digest("hex");
      const history = path.join(t.dir, "usage-sessions");
      fs.mkdirSync(history, { recursive: true });
      const saved = path.join(history, `${identity}.json`);
      const temporary = `${saved}.${process.pid}.tmp`;
      fs.writeFileSync(temporary, JSON.stringify({agent: t.agent, runtime: record.runtime,
        session_id: record.session_id, observed_at: record.ts, launch_id: launch}));
      fs.renameSync(temporary, saved);
    } catch { /* Usage history must not interrupt the agent. */ }
  }
}

// The plan `plan_exit` asks about: the file its question names, relative to the
// worktree, saved where the backbone reads plans (<state_dir>/plans/).
function savePlan(t, question, root) {
  const match = /^Plan at (.+?) is complete\./.exec(question ?? "");
  let text = "";
  try {
    if (match) text = fs.readFileSync(path.resolve(root, match[1]), "utf8");
  } catch { /* shown untitled; the question still waits for an answer */ }
  const title = text.split("\n").map((line) => line.trim().replace(/^#+/, "").trim()).find(Boolean);
  const file = path.join(t.dir, "plans", `${t.agent}.md`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
  return { plan_file: file, plan_title: (title ?? "Untitled plan").slice(0, 120) };
}

function appendAction(t, action) {
  fs.mkdirSync(t.dir, { recursive: true });
  fs.appendFileSync(
    path.join(t.dir, "actions.jsonl"),
    JSON.stringify({ ...action, session: t.agent }) + "\n",
  );
}

// sessionID -> the model its last completed reply came from, in this process;
// a session resumed by a new process keeps the one its state file holds.
const models = new Map();
// sessionID -> {request_error, model_changed} from its completed replies, the
// same way.
const outcomes = new Map();
const OUTCOMES = ["request_error", "model_changed"];

function requestError(error) {
  // Only the name and HTTP status: the message and body are the provider's text.
  const status = error.data?.statusCode;
  return {
    name: error.name,
    ...(Number.isInteger(status) && status >= 400 && status <= 599 ? { status } : {}),
  };
}

function record(t, event, state, reason, extra = {}) {
  const current = readCurrent(t);
  const now = Date.now() / 1000;
  const out = {
    runtime: "opencode",
    state,
    reason: reason ?? null,
    issue: current.issue ?? null,
    repo: current.repo ?? null,
    ts: now,
    started_at: current.started_at ?? now,
    event,
    ...extra,
  };
  if (current.session_id && !out.session_id) out.session_id = current.session_id;
  if (models.has(out.session_id)) out.model = models.get(out.session_id);
  else if (current.model && current.session_id === out.session_id) out.model = current.model;
  const outcome = outcomes.get(out.session_id) ?? (current.session_id === out.session_id ? current : {});
  for (const key of OUTCOMES) if (outcome[key]) out[key] = outcome[key];
  if (current.last_message !== undefined && out.last_message === undefined) {
    out.last_message = current.last_message;
  }
  // When the current turn began, kept through its records (the steer check's
  // receipt that another turn started).
  if (current.prompted_at !== undefined && out.prompted_at === undefined) {
    out.prompted_at = current.prompted_at;
  }
  writeState(t, out);
}

const SKILLS_RELOAD_POLL_MS = 3000;
// One check per process however often OpenCode builds the plugin again (each
// instance does): the newest instance's check is the one the timer runs.
const reloader = (globalThis[Symbol.for("agent-backbone.skills-reload")] ??= {
  check: null,
  loadedAt: Date.now(),
  timer: null,
});

export const AgentBackbone = async ({ client, directory, worktree } = {}) => {
  const t = target();
  if (!t) return {};
  const children = new Map(); // subagent session → its parent; never the agent's own state
  const pending = new Map(); // permission and plan requests waiting for an answer -> who asked
  const held = new Map(); // session whose turn ended while a request waited -> what worked then
  const planCalls = new Set(); // plan_exit calls whose question is still to come
  const turns = new Map(); // sessionID -> what drives its turn: agent, model, ...
  const roots = new Set(); // sessions known to have no parent
  const working = new Set(); // sessions busy in a turn right now
  const lookups = new Map(); // sessionID -> its pending parent lookup
  let root = null;

  const props = (event) => event.properties ?? event.data ?? {};
  const isChild = (sessionID) => sessionID && children.has(sessionID);
  // A subagent resumed from an earlier run (the task tool's task_id) emits no
  // session.created here: ask OpenCode for a session's parent, once. Until it
  // answers, the session is handed nothing and ends no turn.
  const learn = (sessionID) => {
    if (!sessionID || children.has(sessionID) || roots.has(sessionID)) return Promise.resolve();
    if (!lookups.has(sessionID)) {
      lookups.set(sessionID, (async () => {
        try {
          const info = (await client?.session?.get?.({ path: { id: sessionID } }))?.data;
          if (info?.parentID) {
            children.set(sessionID, info.parentID);
            working.delete(sessionID); // its idle is ignored from now on
            held.delete(sessionID);
            withdraw(sessionID);
            // The conversation it works for, as OpenCode names it: never another one.
            // An ancestor not known yet is looked up in turn; when it proves to be
            // a subagent too, its lookup carries the record further up.
            const top = topOf(children, sessionID);
            reclaim(top);
            if (!roots.has(top)) learn(top);
          } else if (info?.id) {
            roots.add(sessionID);
          }
        } catch { /* unknown stays unknown */ }
        lookups.delete(sessionID);
      })());
    }
    return lookups.get(sessionID);
  };
  // A subagent's busy named it while it was unknown: name the agent's own session.
  const reclaim = (own) => {
    const current = readCurrent(t);
    if (!own || !children.has(current.session_id)) return;
    // Only the session changes: a reload's record stays the reload's to end.
    const reload = current.event === "skills.reload" ? { event: current.event, ts: current.ts } : {};
    record(t, "session.subagent", current.state ?? STATE_BUSY, current.reason, { session_id: own, ...reload });
  };

  const handOver = async (sessionID, output) => {
    // A call without a result (a failed subtask) could not carry the fallback.
    if (!output || typeof output !== "object") return;
    if (!offerDirs(t).some((dir) => offers(dir).length > 0)) return;
    await learn(sessionID);
    // Only the agent's own session, and only while its turn runs: a call that
    // ends after the turn did (an interrupt) leaves the offers to that end.
    if (!roots.has(sessionID) || !working.has(sessionID)) return;
    const texts = takeContext(t, children);
    if (texts.length === 0) return;
    const text = texts.join("\n\n");
    // A message without what drives the turn (its agent and model) would switch it.
    const turn = turns.get(sessionID);
    if (turn && client?.session?.prompt) {
      try {
        const result = await client.session.prompt({
          path: { id: sessionID },
          body: { noReply: true, ...turn, parts: [{ type: "text", text }] },
        });
        if (!result?.error) return;
      } catch { /* the tool's result carries it instead */ }
    }
    // Already taken: never drop it. It rides on this tool's result, which
    // for an MCP tool is its content blocks.
    if (Array.isArray(output.content)) output.content.push({ type: "text", text });
    else output.output = output.output ? `${output.output}\n\n${text}` : text;
  };

  // The turn ends when nothing works any more, whether or not the session's
  // parent is known yet; a subagent that finishes while it goes on ends nothing.
  const endTurn = (event, sessionID) => {
    working.delete(sessionID);
    if (roots.has(sessionID)) working.clear(); // its subagents are done too
    if (working.size > 0) return;
    turns.clear(); // the next turn's requests say what drives it
    record(t, event, STATE_IDLE, null, { session_id: sessionID });
    retireSteers(t);
  };

  // A turn's end waits while a request does; it counts once none waits.
  const idle = (event, sessionID) => {
    if (pending.size === 0) endTurn(event, sessionID);
    else held.set(sessionID, new Set(working));
  };

  // Its requests answered, the agent's work goes on: a turn's end seen while
  // they waited is not counted.
  const resume = (event, sessionID) => {
    if (pending.size > 0) return;
    held.clear();
    record(t, event, STATE_BUSY, null, { session_id: sessionID });
  };

  // A session that asked before it was known as a subagent: its requests were
  // never the agent's, and a turn that ended while they waited ends now.
  const withdraw = (sessionID) => {
    const asked = [...pending].filter(([, by]) => by === sessionID);
    if (asked.length === 0) return;
    for (const [id] of asked) pending.delete(id);
    if (pending.size > 0) return;
    const ended = [...held.keys()];
    for (const [id, was] of held) {
      working.delete(id);
      // Its subagents are done too; work begun since goes on.
      if (roots.has(id)) for (const other of was) working.delete(other);
    }
    held.clear();
    const own = (ids) => ids.find((id) => roots.has(id)) ?? ids[0];
    const event = "session.subagent";
    if (working.size === 0) endTurn(event, own(ended) ?? root ?? sessionID);
    else record(t, event, STATE_BUSY, null, { session_id: own([...working]) });
  };

  let reloading = false;
  let disposed = false;
  const checkReload = async () => {
    if (disposed) return;
    const marker = path.join(t.dir, `${t.agent}.skills-reload`);
    let changedAt;
    try {
      changedAt = fs.statSync(marker).mtimeMs;
    } catch {
      return; // nothing changed
    }
    if (reloading || working.size > 0 || pending.size > 0) return; // between turns only
    try {
      fs.unlinkSync(marker);
    } catch {
      return;
    }
    if (changedAt < reloader.loadedAt) return; // this process started after: it read them
    reloading = true;
    let mine; // when this reload's busy record was written
    try {
      // Busy while it reloads, so the backbone delivers nothing into it.
      record(t, "skills.reload", STATE_BUSY, null);
      mine = readCurrent(t).ts;
      await client?.instance?.dispose?.();
    } catch {
      /* the skills stay as they were until the next change or start */
    } finally {
      try {
        // The plugin OpenCode builds again while disposing may have recorded a
        // turn since (in the same millisecond, too): only our own record ends.
        const now = readCurrent(t);
        if (now.event === "skills.reload" && now.ts === mine) record(t, "skills.reload", STATE_IDLE, null);
      } catch { /* hook failures must not stop the agent */ }
      reloading = false;
    }
  };
  reloader.check = checkReload;
  if (!reloader.timer) {
    reloader.timer = setInterval(() => reloader.check?.(), SKILLS_RELOAD_POLL_MS);
    reloader.timer.unref?.();
  }

  return {
    dispose() {
      disposed = true;
      if (reloader.check === checkReload) reloader.check = null;
    },
    "chat.params": async (input) => {
      // The user message this request answers drives the turn.
      const message = input?.message;
      if (!input?.sessionID || !message?.agent || !message?.model?.modelID) return;
      const { providerID, modelID, variant } = message.model;
      const set = ["system", "tools", "format"].filter((key) => message[key] != null);
      turns.set(input.sessionID, {
        agent: message.agent,
        model: { providerID, modelID },
        ...(variant ? { variant } : {}),
        ...Object.fromEntries(set.map((key) => [key, message[key]])),
      });
    },
    event: async ({ event }) => {
      const p = props(event);
      switch (event.type) {
        case "session.created": {
          const info = p.info ?? {};
          if (info.parentID) children.set(info.id, info.parentID);
          else {
            roots.add(info.id);
            if (!root) root = info.id;
          }
          return;
        }
        case "session.status": {
          if (isChild(p.sessionID)) return;
          const type = p.status?.type;
          if (type === "busy") {
            // A turn starts when nothing was working: the steer check's receipt.
            const starts = working.size === 0;
            working.add(p.sessionID);
            held.delete(p.sessionID);
            for (const was of held.values()) was.delete(p.sessionID);
            learn(p.sessionID);
            record(t, event.type, STATE_BUSY, null, {
              session_id: p.sessionID,
              ...(starts ? { prompted_at: Date.now() / 1000 } : {}),
            });
          } else if (type === "idle") idle(event.type, p.sessionID);
          return;
        }
        case "session.idle": {
          if (isChild(p.sessionID)) return;
          idle(event.type, p.sessionID);
          return;
        }
        case "message.updated": {
          const info = p.info ?? {};
          if (isChild(info.sessionID) || info.role !== "assistant" || !info.time?.completed) return;
          if (info.error?.name === "MessageAbortedError") return;
          const current = readCurrent(t);
          const resumed = current.session_id === info.sessionID ? current : {};
          const seen = outcomes.get(info.sessionID) ?? resumed;
          if (info.error) {
            const request_error = requestError(info.error);
            outcomes.set(info.sessionID, { model_changed: seen.model_changed, request_error });
            return;
          }
          if (!info.finish) return; // no model answered: a shell command the user ran
          const outcome = { model_changed: seen.model_changed }; // no error: the last one is cleared
          if (info.providerID && info.modelID) {
            const model = `${info.providerID}/${info.modelID}`;
            const before = models.get(info.sessionID) ?? resumed.model;
            if (before && before !== model) outcome.model_changed = model;
            models.set(info.sessionID, model);
          }
          outcomes.set(info.sessionID, outcome);
          return;
        }
        case "session.error": {
          if (isChild(p.sessionID)) return;
          record(t, event.type, STATE_IDLE, null);
          return;
        }
        case "permission.asked": {
          if (isChild(p.sessionID)) return;
          pending.set(p.id ?? p.requestID ?? "?", p.sessionID);
          record(t, event.type, STATE_WAITING, REASON_PERMISSION, { session_id: p.sessionID });
          learn(p.sessionID); // a subagent's that is not known yet withdraws it
          return;
        }
        case "permission.replied": {
          if (isChild(p.sessionID)) return;
          pending.delete(p.requestID ?? p.id ?? "?");
          resume(event.type, p.sessionID);
          return;
        }
        case "question.asked": {
          // Only plan_exit's question is a plan decision; the agent's own session asked it.
          if (!planCalls.delete(p.tool?.callID)) return;
          pending.set(p.id, p.sessionID);
          const plan = savePlan(t, p.questions?.[0]?.question, worktree ?? directory ?? process.cwd());
          record(t, event.type, STATE_WAITING, REASON_PLAN, { session_id: p.sessionID, ...plan });
          return;
        }
        case "question.replied":
        case "question.rejected": {
          if (!pending.delete(p.requestID)) return;
          resume(event.type, p.sessionID);
          return;
        }
        default:
          return;
      }
    },
    "tool.execute.before": async (input, output) => {
      if (isChild(input?.sessionID)) return;
      if (input?.tool === "plan_exit") planCalls.add(input.callID);
      if (input?.tool !== "bash") return;
      const command = output?.args?.command;
      if (typeof command !== "string") return;
      for (const action of await shellActions(command, directory ?? process.cwd(), "intent")) appendAction(t, action);
    },
    "tool.execute.after": async (input, output) => {
      if (isChild(input?.sessionID)) return;
      await handOver(input?.sessionID, output);
      if (input?.tool !== "bash") return;
      if (output?.metadata?.exit !== 0 || output?.metadata?.timeout) return;
      const command = input?.args?.command;
      if (typeof command !== "string") return;
      for (const action of await shellActions(command, directory ?? process.cwd(), "succeeded")) appendAction(t, action);
    },
  };
};
