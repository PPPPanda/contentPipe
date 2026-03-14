import path from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";

export type ContentPipeInprocessConfig = {
  enabled: boolean;
  workspaceRoot: string;
  outputDir: string;
  allowSubagentBridge: boolean;
};

export function resolveInprocessConfig(api: OpenClawPluginApi): ContentPipeInprocessConfig {
  const cfg = (api.pluginConfig ?? {}) as Partial<ContentPipeInprocessConfig>;
  const sourceDir = path.dirname(api.source);
  // plugin-inprocess/ lives under the ContentPipe repo root, so repo root is one level up.
  const workspaceRoot = cfg.workspaceRoot || path.resolve(sourceDir, "..");
  return {
    enabled: cfg.enabled ?? true,
    workspaceRoot,
    outputDir: cfg.outputDir || path.join(workspaceRoot, "output"),
    allowSubagentBridge: cfg.allowSubagentBridge ?? true,
  };
}
