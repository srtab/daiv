import type { ToolRenderProps } from "./types";

export function LsTool({ args, status, result }: ToolRenderProps) {
  const path = (args.path as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        ls <span className="chat-tool__path">{path}</span>
      </summary>
      <pre className="chat-tool__code">{String(result ?? "")}</pre>
    </details>
  );
}

export function GlobTool({ args, status, result }: ToolRenderProps) {
  const pattern = (args.pattern as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        glob <code>{pattern}</code>
      </summary>
      <pre className="chat-tool__code">{String(result ?? "")}</pre>
    </details>
  );
}
