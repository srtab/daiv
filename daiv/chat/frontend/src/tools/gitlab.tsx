import type { ToolRenderProps } from "./types";

export function GitlabTool({ args, status, result }: ToolRenderProps) {
  const command = (args.command as string) ?? (args.subcommand as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        gitlab <code>{command}</code>
      </summary>
      {result != null && <pre className="chat-tool__code">{String(result)}</pre>}
    </details>
  );
}
