import type { ToolRenderProps } from "./types";

export function WebFetchTool({ args, status, result }: ToolRenderProps) {
  const url = (args.url as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        <a href={url}>{url}</a>
      </summary>
      {result != null && <pre className="chat-tool__code">{String(result)}</pre>}
    </details>
  );
}
