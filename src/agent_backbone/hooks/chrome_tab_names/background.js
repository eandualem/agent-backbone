// Names each tab group Claude-in-Chrome opened for a Backbone agent after that
// agent. The Backbone hook records which group and tabs an agent's session
// uses; the native host hands those records over. A group is renamed only
// while it still has Claude's default title and still holds one of the
// recorded tabs: a group a person renamed, or anyone else's group, is never
// touched. Nothing here reads or changes the Claude extension itself.

const HOST = "com.agent_backbone.tab_names";
const DEFAULT_TITLE = /^(⌛\s*)?Claude$/;

export async function nameGroups(api = chrome) {
  let reply;
  try {
    reply = await api.runtime.sendNativeMessage(HOST, { op: "groups" });
  } catch {
    return 0; // host not installed or Backbone data missing: leave titles alone
  }
  let renamed = 0;
  for (const entry of reply?.groups ?? []) {
    let group;
    try {
      group = await api.tabGroups.get(entry.group);
    } catch {
      continue; // closed, or from an earlier browser session
    }
    const match = DEFAULT_TITLE.exec(group.title ?? "");
    if (!match) continue;
    const tabs = await api.tabs.query({ groupId: entry.group });
    if (!tabs.some((tab) => entry.tabs.includes(tab.id))) continue;
    await api.tabGroups.update(entry.group, { title: (match[1] ?? "") + entry.title });
    renamed += 1;
  }
  return renamed;
}

let pending;
function soon() {
  // Groups appear before the agent's hook records them: check again shortly.
  clearTimeout(pending);
  pending = setTimeout(() => {
    nameGroups();
    setTimeout(() => nameGroups(), 5000);
  }, 1000);
}

// Chrome passes each listener its own argument (an Alarm, install details):
// never let it stand in for the API.
const sync = () => {
  nameGroups();
};

if (globalThis.chrome?.tabGroups) {
  chrome.tabGroups.onCreated.addListener(soon);
  chrome.tabGroups.onUpdated.addListener(soon);
  chrome.tabs.onUpdated.addListener(soon);
  chrome.alarms.create("name-groups", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener(sync);
  chrome.runtime.onStartup.addListener(sync);
  chrome.runtime.onInstalled.addListener(sync);
}
