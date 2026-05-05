import type { ToolRenderProps, ToolRenderer } from "./types";
import { FallbackTool } from "./fallback";
import { ReadFileTool, WriteFileTool, EditFileTool } from "./file_ops";
import { WriteTodosTool } from "./todos";
import { GrepTool } from "./grep";
import { LsTool, GlobTool } from "./ls_glob";
import { BashTool } from "./bash";
import { TaskTool } from "./task";
import { SkillTool } from "./skill";
import { WebFetchTool } from "./web_fetch";
import { WebSearchTool } from "./web_search";
import { GitlabTool } from "./gitlab";
import { GhTool } from "./gh";
import { PublishPhaseChip, PUBLISH_PHASE_LABELS } from "./publish_phase";

const REGISTRY: Record<string, ToolRenderer> = {};

export function registerTool(name: string, renderer: ToolRenderer): void {
  REGISTRY[name] = renderer;
}

export function renderTool(props: ToolRenderProps) {
  const renderer = REGISTRY[props.name] ?? FallbackTool;
  return renderer(props);
}

registerTool("read_file", ReadFileTool);
registerTool("write_file", WriteFileTool);
registerTool("edit_file", EditFileTool);
registerTool("write_todos", WriteTodosTool);
registerTool("grep", GrepTool);
registerTool("ls", LsTool);
registerTool("glob", GlobTool);
registerTool("bash", BashTool);
registerTool("task", TaskTool);
registerTool("skill", SkillTool);
registerTool("web_fetch", WebFetchTool);
registerTool("web_search", WebSearchTool);
registerTool("gitlab", GitlabTool);
registerTool("gh", GhTool);
for (const name of Object.keys(PUBLISH_PHASE_LABELS)) registerTool(name, PublishPhaseChip);
