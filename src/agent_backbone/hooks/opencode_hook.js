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
import { execFileSync } from "node:child_process";

const STATE_IDLE = "idle";
const STATE_BUSY = "busy";
const STATE_WAITING = "waiting_for_human";
const REASON_PERMISSION = "permission";
const GH_COMMENT = /\bgh\s+issue\s+comment\s+(?:\S+\s+)*?(\d+)\b/;
const GH_REPO = /(?:--repo|-R)[\s=]+([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)/;
const GH_PR_CREATE = /\bgh\s+pr\s+create\b/;
const GH_HEAD = /(?:--head|-H)[\s=]+(\S+)/;
const REMOTE = /github\.com[:/]([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?)(?:\.git)?\/?$/;

function git(cwd, args) {
  try {
    return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf8", timeout: 5000 }).trim();
  } catch {
    return null;
  }
}

function shellActions(command, cwd) {
  const actions = [];
  const comment = GH_COMMENT.exec(command);
  if (comment) {
    const action = { ts: Date.now() / 1000, action: "comment", issue: Number(comment[1]) };
    const repo = GH_REPO.exec(command);
    if (repo) action.repo = repo[1];
    actions.push(action);
  }
  if (GH_PR_CREATE.test(command)) {
    // Same identity the Python hooks record: the head repository (the
    // checkout's origin, or the owner named by --head owner:branch) and branch.
    const action = { ts: Date.now() / 1000, action: "pull_request" };
    const remote = REMOTE.exec(git(cwd, ["remote", "get-url", "origin"]) ?? "");
    const origin = remote ? remote[1] : null;
    const repoFlag = GH_REPO.exec(command);
    const repo = repoFlag ? repoFlag[1] : origin;
    let headRepo = origin;
    let branch;
    const headFlag = GH_HEAD.exec(command);
    if (headFlag) {
      const value = headFlag[1];
      const colon = value.lastIndexOf(":");
      if (colon > 0) {
        const baseName = (repo ?? origin ?? "").split("/").pop();
        headRepo = baseName ? `${value.slice(0, colon)}/${baseName}` : null;
        branch = value.slice(colon + 1);
      } else {
        branch = value;
      }
    } else {
      branch = git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]);
    }
    if (repo) action.repo = repo;
    if (headRepo) action.head_repo = headRepo;
    if (branch) action.branch = branch;
    actions.push(action);
  }
  return actions;
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
      if (isChild(input?.sessionID)) return;
      const command = output?.args?.command;
      if (typeof command !== "string") return;
      for (const action of shellActions(command, directory ?? process.cwd())) appendAction(t, action);
    },
  };
};
