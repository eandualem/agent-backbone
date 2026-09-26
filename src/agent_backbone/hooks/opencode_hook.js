// OpenCode plugin: push agent state to the agent-backbone state directory.
//
// Wired at launch through OPENCODE_CONFIG_CONTENT ({"plugin": ["file://…"]}),
// which OpenCode merges on top of the user's own configuration, so nothing
// in ~/.config/opencode or the repository is touched. Verified against
// OpenCode 1.18 in its TUI: `session.status` carries {type: "busy" | "idle"},
// `session.idle` ends a turn, `permission.asked` / `permission.replied`
// bracket a permission dialog. (`opencode run` exits before plugin handlers
// finish; the backbone only starts the TUI.)
//
// Writes the same <state_dir>/<agent>.json the Python hooks write. Only the
// root session counts: sessions with a parentID are OpenCode's own subagents.
//
// What the backbone offers a working agent (a steer, a high-priority batch;
// <state_dir>/context/, see backbone_state.py) is taken after each tool call
// and joins the running turn as a user message, OpenCode's own path for
// input typed while it works: the turn's next step reads it (verified live
// against OpenCode 1.18).

import fs from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";

const STATE_IDLE = "idle";
const STATE_BUSY = "busy";
const STATE_WAITING = "waiting_for_human";
const REASON_PERMISSION = "permission";
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

function takeContext(t) {
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
  for (const dir of offerDirs(t)) {
    for (const name of offers(dir).sort(order)) {
      const offer = path.join(dir, name);
      try {
        // Read first: once it is .taken the backbone may settle and remove it.
        const text = fs.readFileSync(offer, "utf8");
        fs.renameSync(offer, `${offer.slice(0, -3)}.taken`);
        texts.push(text);
      } catch { /* the backbone claimed it first, or it is gone */ }
    }
  }
  return texts;
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

function readCurrent(t) {
  try {
    return JSON.parse(fs.readFileSync(path.join(t.dir, `${t.agent}.json`), "utf8"));
  } catch {
    return {};
  }
}

function writeState(t, record) {
  fs.mkdirSync(t.dir, { recursive: true });
  const file = path.join(t.dir, `${t.agent}.json`);
  const tmp = path.join(t.dir, `.${t.agent}.json.${process.pid}.tmp`);
  fs.writeFileSync(tmp, JSON.stringify(record));
  fs.renameSync(tmp, file);
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

function appendAction(t, action) {
  fs.mkdirSync(t.dir, { recursive: true });
  fs.appendFileSync(
    path.join(t.dir, "actions.jsonl"),
    JSON.stringify({ ...action, session: t.agent }) + "\n",
  );
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

export const AgentBackbone = async ({ client, directory } = {}) => {
  const t = target();
  if (!t) return {};
  const children = new Set(); // subagent sessions, never the agent's own state
  const pending = new Set(); // permission requests waiting for an answer
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
            children.add(sessionID);
            working.delete(sessionID); // its idle is ignored from now on
          } else if (info?.id) roots.add(sessionID);
        } catch { /* unknown stays unknown */ }
        lookups.delete(sessionID);
      })());
    }
    return lookups.get(sessionID);
  };

  const handOver = async (sessionID, output) => {
    // A call without a result (a failed subtask) could not carry the fallback.
    if (!output || typeof output !== "object") return;
    if (!offerDirs(t).some((dir) => offers(dir).length > 0)) return;
    await learn(sessionID);
    // Only the agent's own session, and only while its turn runs: a call that
    // ends after the turn did (an interrupt) leaves the offers to that end.
    if (!roots.has(sessionID) || !working.has(sessionID)) return;
    const texts = takeContext(t);
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

  const endTurn = (event, sessionID) => {
    working.delete(sessionID);
    record(t, event, STATE_IDLE, null, { session_id: sessionID });
    if (roots.has(sessionID)) retireSteers(t);
  };

  return {
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
          if (info.parentID) children.add(info.id);
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
            learn(p.sessionID);
            record(t, event.type, STATE_BUSY, null, {
              session_id: p.sessionID,
              ...(starts ? { prompted_at: Date.now() / 1000 } : {}),
            });
          } else if (type === "idle" && pending.size === 0) endTurn(event.type, p.sessionID);
          return;
        }
        case "session.idle": {
          if (isChild(p.sessionID)) return;
          if (pending.size === 0) endTurn(event.type, p.sessionID);
          return;
        }
        case "session.error": {
          if (isChild(p.sessionID)) return;
          record(t, event.type, STATE_IDLE, null);
          return;
        }
        case "permission.asked": {
          if (isChild(p.sessionID)) return;
          pending.add(p.id ?? p.requestID ?? "?");
          record(t, event.type, STATE_WAITING, REASON_PERMISSION, { session_id: p.sessionID });
          return;
        }
        case "permission.replied": {
          if (isChild(p.sessionID)) return;
          pending.delete(p.requestID ?? p.id ?? "?");
          if (pending.size === 0) record(t, event.type, STATE_BUSY, null, { session_id: p.sessionID });
          return;
        }
        default:
          return;
      }
    },
    "tool.execute.before": async (input, output) => {
      if (isChild(input?.sessionID) || input?.tool !== "bash") return;
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
