import type { ToolRenderProps } from "./types";

export function TaskTool({ args, status, result }: ToolRenderProps) {
  const sub = (args.subagent_type as string) ?? "subagent";
  const desc = (args.description as string) ?? "";
  return (
    <details className="chat-tool chat-tool--task" data-status={status}>
      <summary>
        <span className="chat-tool__badge" data-tone="info">
          {sub}
        </span>
        {desc && <span className="chat-tool__path">{desc}</span>}
      </summary>
      {result != null && <pre className="chat-tool__code">{String(result)}</pre>}
    </details>
  );
}
