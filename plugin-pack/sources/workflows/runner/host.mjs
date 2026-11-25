// The vendored runtime's WorkflowHost, forwarded over the bridge: the runner spawns nothing itself.
import { Refusal } from "./prescan.mjs";

const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);

/** A gate outcome `{ok, output}` the runtime can trust, or null. */
const gateOutcome = (value) => (isObject(value) && typeof value.ok === "boolean"
  ? { ok: value.ok, output: String(value.output ?? "") } : null);

/** A host answer the runtime can trust, or a failed agent naming what was wrong with it. */
function agentResult(result, op) {
  const gate = isObject(result) ? gateOutcome(result.gate) : null;
  if (!isObject(result) || typeof result.ok !== "boolean" || (result.gate !== undefined && gate === null)) {
    return { ok: false, error: `the host answered ${op} with ${JSON.stringify(result)}` };
  }
  return result.gate === undefined ? result : { ...result, gate };
}

/** Spawn request on the wire: the compiled schema as the script wrote it, the callback dropped. */
function wireRequest(request) {
  const { onResolved: _callback, schema, ...rest } = request;
  return schema === undefined ? rest : { ...rest, schema: schema.schema };
}

export function createStdioHost(bridge, { prescan }) {
  return {
    async spawnAgent(request) {
      return agentResult(await bridge.call("spawn", { req: wireRequest(request) }), "spawn");
    },
    abortAgent(agentId) {
      bridge.notify({ op: "abort", agent: agentId });
    },
    async resumeAgent(agentId, prompt) {
      return agentResult(await bridge.call("resume", { agent: agentId, prompt }), "resume");
    },
    async runGate(command, { agentId, cwd }) {
      const result = await bridge.call("gate", { command, agent: agentId, ...(cwd !== undefined ? { cwd } : {}) });
      return gateOutcome(result) ?? { ok: false, output: `the host answered gate with ${JSON.stringify(result)}` };
    },
    async loadWorkflow(ref) {
      const source = await bridge.call("load", { ref });
      if (!isObject(source) || source.ok !== true || typeof source.script !== "string") {
        return { ok: false, message: String(source?.message ?? `no workflow for ${JSON.stringify(ref)}`) };
      }
      try {
        prescan(source.script);
      } catch (error) {
        if (error instanceof Refusal) return { ok: false, message: error.message };
        throw error;
      }
      return source;
    },
  };
}
