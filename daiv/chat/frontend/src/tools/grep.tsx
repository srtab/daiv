import type { ToolRenderProps } from "./types";

export function GrepTool({ args, status, result }: ToolRenderProps) {
  const pattern = (args.pattern as string) ?? "";
  const path = (args.path as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        grep <code>{pattern}</code> {path && <span className="chat-tool__path">{path}</span>}
      </summary>
      <pre className="chat-tool__code">{String(result ?? "")}</pre>
    </details>
  );
}
