import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { resolveInprocessConfig } from "./config.js";
import { inspectRuntimeBridge, runSubagentBridgeProbe } from "./runtime-bridge.js";

export function createContentPipePlugin(api: OpenClawPluginApi) {
  const cfg = resolveInprocessConfig(api);
  const runtimeReport = inspectRuntimeBridge(api);

  api.logger.info(`[contentpipe-inprocess] scaffold loaded; runtime=${runtimeReport.runtimeVersion}`);
  api.logger.info(`[contentpipe-inprocess] ${runtimeReport.note}`);

  api.registerHttpRoute({
    path: "/contentpipe-inprocess/health",
    auth: "gateway",
    match: "exact",
    handler: async (_req, res) => {
      res.statusCode = 200;
      res.setHeader("content-type", "application/json; charset=utf-8");
      res.end(JSON.stringify({
        ok: true,
        plugin: "content-pipeline-inprocess",
        phase: 0,
        config: cfg,
        runtime: runtimeReport,
      }));
      return true;
    },
  });

  api.registerCommand({
    name: "contentpipe-runtime-status",
    description: "Show ContentPipe in-process migration runtime status.",
    acceptsArgs: false,
    handler: async (_ctx) => {
      const probe = await runSubagentBridgeProbe(api);
      return {
        text: [
          "ContentPipe In-Process Scaffold",
          `- runtime.version: ${runtimeReport.runtimeVersion}`,
          `- runtime.llm available: ${runtimeReport.hasDirectLlm ? 'yes' : 'no'}`,
          `- runtime.subagent available: ${runtimeReport.hasSubagent ? 'yes' : 'no'}`,
          `- bridge probe: ${probe.ok ? 'ok' : 'failed'} — ${probe.detail}`,
          `- workspaceRoot: ${cfg.workspaceRoot}`,
          `- outputDir: ${cfg.outputDir}`,
        ].join("\n"),
      };
    },
  });
}

