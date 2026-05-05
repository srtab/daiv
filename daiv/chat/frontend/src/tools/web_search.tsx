import type { ToolRenderProps } from "./types";

export function WebSearchTool({ args, status, result }: ToolRenderProps) {
  const query = (args.query as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        <code>{query}</code>
      </summary>
      {result != null && <pre className="chat-tool__code">{String(result)}</pre>}
    </details>
  );
}
