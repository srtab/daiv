import type { ToolRenderProps } from "./types";

export function SkillTool({ args, status }: ToolRenderProps) {
  const skill = (args.skill as string) ?? "";
  return (
    <div className="chat-tool chat-tool--skill" data-status={status}>
      <span className="chat-tool__badge" data-tone="info">
        skill
      </span>
      <code>{skill}</code>
    </div>
  );
}
