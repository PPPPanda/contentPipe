import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { createContentPipePlugin } from "./src/plugin.js";

const plugin = {
  id: "content-pipeline-inprocess",
  name: "ContentPipe In-Process",
  description: "Phase-0 in-process plugin scaffold for ContentPipe runtime migration.",
  register(api: OpenClawPluginApi) {
    createContentPipePlugin(api);
  },
};

export default plugin;
