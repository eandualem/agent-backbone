// OpenCode v2 plugin. The launcher installs this as a local directory's index.js.
// Reuse the state/offer protocol; translate only the native v2 API boundaries.
import { AgentBackbone } from "../opencode_hook.js";

export default {
  id: "agent-backbone",
  async setup(ctx) {
    const steps = new Map();
    const hook = await AgentBackbone({
      directory: ctx.location.directory,
      client: {
        session: {
          get: async ({ path }) => ({ data: await ctx.session.get({ sessionID: path.id }) }),
          prompt: ({ path, body }) => ctx.session.synthetic({
            sessionID: path.id,
            text: body.parts.map((part) => part.text).join("\n\n"),
            delivery: "steer",
            resume: false,
          }),
        },
        instance: { dispose: () => ctx.skill.reload() },
      },
    });
    if (!hook.event) return;
    const dispatch = (type, properties) => hook.event({ event: { type, properties } });
    const receive = async (event) => {
      const data = event.data ?? {};
      switch (event.type) {
        case "session.created":
          return dispatch(event.type, { info: { id: data.sessionID, parentID: data.parentID } });
        case "session.step.started":
          steps.set(data.assistantMessageID, data.model);
          return;
        case "session.step.ended":
        case "session.step.failed": {
          const model = steps.get(data.assistantMessageID);
          steps.delete(data.assistantMessageID);
          return dispatch("message.updated", { info: {
            role: "assistant",
            sessionID: data.sessionID,
            providerID: model?.providerID,
            modelID: model?.id,
            time: { completed: event.created },
            finish: data.finish,
            ...(data.error ? { error: {
              name: data.error.type,
              data: { statusCode: data.error.status },
            } } : {}),
          } });
        }
        case "session.execution.failed":
          return dispatch("session.error", data);
        case "session.execution.interrupted":
          return dispatch("session.idle", data);
        case "session.status":
        case "session.idle":
        case "permission.asked":
        case "permission.replied":
          return dispatch(event.type, data);
      }
    };
    await ctx.session.hook("context", async (event) => {
      await hook["chat.params"]({ sessionID: event.sessionID, message: {
        agent: event.agent,
        model: { providerID: event.model.providerID, modelID: event.model.id },
      } });
    });
    const input = (event) => ({
      sessionID: event.sessionID,
      tool: event.tool === "shell" ? "bash" : event.tool,
      callID: event.id,
      args: event.input,
    });
    await ctx.tool.hook("execute.before", (event) =>
      hook["tool.execute.before"](input(event), { args: event.input }));
    await ctx.tool.hook("execute.after", async (event) => {
      if (event.status !== "completed") return;
      // A failed synthetic admission still delivers the offer with this result.
      const result = event.result;
      const output = {
        metadata: result.metadata,
        content: typeof result.content === "string"
          ? [{ type: "text", text: result.content }]
          : [...(result.content ?? [])],
      };
      const length = output.content.length;
      try { await hook["tool.execute.after"](input(event), output); } catch {
        // Already-taken context must survive a later action-log write failure.
      }
      if (output.content.length > length) {
        if (result.content === undefined && result.output !== undefined) {
          output.content.unshift({ type: "text", text: JSON.stringify(result.output) });
        }
        event.result = { ...result, content: output.content };
      }
    });
    const controller = new AbortController();
    const events = (async () => {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        // A transient state-file failure must not drop every subsequent event.
        try { await receive(event); } catch {}
      }
    })();
    // Native plugin teardown owns the subscription; it must not survive a reload.
    events.catch(() => {});
    return async () => {
      controller.abort();
      await events.catch(() => {});
    };
  },
};
