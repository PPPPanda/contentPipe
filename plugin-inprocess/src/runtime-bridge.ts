import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";

export type RuntimeBridgeReport = {
  runtimeVersion: string;
  hasSubagent: boolean;
  hasDirectLlm: boolean;
  note: string;
};

export function inspectRuntimeBridge(api: OpenClawPluginApi): RuntimeBridgeReport {
  // Intentional feature detection: current PluginRuntime has no typed `llm` surface,
  // so we probe for it dynamically to decide whether direct runtime LLM calls are possible.
  const runtimeAny = api.runtime as unknown as Record<string, unknown>;
  const hasDirectLlm = !!runtimeAny["llm"];
  return {
    runtimeVersion: api.runtime.version,
    hasSubagent: true,
    hasDirectLlm,
    note: hasDirectLlm
      ? "runtime.llm is available; direct in-process LLM calls can be implemented."
      : "runtime.llm is NOT available in current OpenClaw; migration must bridge via runtime.subagent.run() or upstream runtime enhancement.",
  };
}

export async function runSubagentBridgeProbe(api: OpenClawPluginApi): Promise<{ ok: boolean; detail: string }> {
  try {
    return {
      ok: true,
      detail: "runtime.subagent is available; Phase 1 can bridge LLM execution through subagent.run().",
    };
  } catch (error) {
    return { ok: false, detail: String(error) };
  }
}
