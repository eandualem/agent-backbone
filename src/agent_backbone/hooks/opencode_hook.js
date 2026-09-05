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

import fs from "node:fs";
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
  writeState(t, out);
}

export const AgentBackbone = async ({ directory } = {}) => {
  const t = target();
  if (!t) return {};
  const children = new Set(); // subagent sessions, never the agent's own state
  const pending = new Set(); // permission requests waiting for an answer
  let root = null;

  const props = (event) => event.properties ?? event.data ?? {};
  const isChild = (sessionID) => sessionID && children.has(sessionID);

  return {
    event: async ({ event }) => {
      const p = props(event);
      switch (event.type) {
        case "session.created": {
          const info = p.info ?? {};
          if (info.parentID) children.add(info.id);
          else if (!root) root = info.id;
          return;
        }
        case "session.status": {
          if (isChild(p.sessionID)) return;
          const type = p.status?.type;
          if (type === "busy") record(t, event.type, STATE_BUSY, null, { session_id: p.sessionID });
          else if (type === "idle" && pending.size === 0) {
            record(t, event.type, STATE_IDLE, null, { session_id: p.sessionID });
          }
          return;
        }
        case "session.idle": {
          if (isChild(p.sessionID)) return;
          if (pending.size === 0) record(t, event.type, STATE_IDLE, null, { session_id: p.sessionID });
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
      if (isChild(input?.sessionID) || input?.tool !== "bash") return;
      if (output?.metadata?.exit !== 0 || output?.metadata?.timeout) return;
      const command = input?.args?.command;
      if (typeof command !== "string") return;
      for (const action of await shellActions(command, directory ?? process.cwd(), "succeeded")) appendAction(t, action);
    },
  };
};
