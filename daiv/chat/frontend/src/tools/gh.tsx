import { formatResult, type ToolRenderProps } from "./types";

export function GhTool({ args, status, result }: ToolRenderProps) {
  const command = (args.command as string) ?? (args.subcommand as string) ?? "";
  return (
    <details className="chat-tool" data-status={status}>
      <summary>
        gh <code>{command}</code>
      </summary>
      {result != null && <pre className="chat-tool__code">{formatResult(result)}</pre>}
    </details>
  );
}
