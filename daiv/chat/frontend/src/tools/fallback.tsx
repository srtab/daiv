import type { ToolRenderProps } from "./types";

export function FallbackTool({ name, args, result, status }: ToolRenderProps) {
  return (
    <details className="chat-tool chat-tool--fallback" data-status={status}>
      <summary>{name}</summary>
      <pre>{JSON.stringify({ args, result }, null, 2)}</pre>
    </details>
  );
}
