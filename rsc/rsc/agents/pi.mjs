/** Native MCP registration and lossless, model-facing RSC results. */
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

export function restoreResult(event, namespace) {
  if (!event.toolName.startsWith(`mcp__${namespace}__`)) return;
  const raw = event.structuredContent;
  if (!raw || !Array.isArray(raw.content) || !raw.content.every(c => c.type === "text" && typeof c.text === "string")) {
    throw new Error("RSC MCP result is missing its complete original content");
  }
  return { content: raw.content, structuredContent: raw, isError: !!(raw.isError || event.isError) };
}

export default function extension(pi) {
  const config = JSON.parse(readFileSync(process.env.RSC_PI_RUNTIME, "utf8"));
  let failed = false;
  const emit = (kind, body = {}) => appendFileSync(config.events,
    JSON.stringify({ time: Date.now() / 1000, kind, ...body }) + "\n", { mode: 0o600 });
  const fatal = async (ctx, error) => {
    failed = true;
    const message = String(error.message || error);
    writeFileSync(config.fatal, JSON.stringify({ error: message }), { mode: 0o600 });
    emit("fatal", { error: message });
    await ctx.abort();
    return { content: [{ type: "text", text: message }], structuredContent: undefined, isError: true };
  };
  pi.registerMcpServer(config.namespace, { ...config.bridge, exposure: "direct" });
  pi.on("tool_call", () => failed
    ? { block: true, terminate: true, reason: "RSC adapter failed; no further tools may run" }
    : undefined);
  pi.on("session_start", async (_event, ctx) => {
    writeFileSync(config.ready, JSON.stringify({ namespace: config.namespace }), { mode: 0o600 });
    emit("ready");
    ctx.ui.notify("RSC adapter ready", "info");
  });
  pi.on("before_agent_start", async (_event, ctx) => {
    if (!ctx.model || `${ctx.model.provider}/${ctx.model.id}` !== config.model || pi.getThinkingLevel() !== config.effort) {
      await fatal(ctx, new Error("Pi selected a different model or thinking level than the frozen profile"));
    } else {
      emit("model_verified", { model: config.model, effort: config.effort });
    }
  });
  pi.on("tool_result", async (event, ctx) => {
    try {
      const restored = restoreResult(event, config.namespace);
      if (restored) {
        const content = restored.content.map(c => c.text).join("\n");
        emit("delivered", { tool: event.toolName, bytes: Buffer.byteLength(content),
          sha256: createHash("sha256").update(content).digest("hex"), isError: restored.isError });
      }
      return restored;
    } catch (error) {
      return fatal(ctx, error);
    }
  });
}
