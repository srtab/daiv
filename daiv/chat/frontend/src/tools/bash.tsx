import type { ToolRenderProps } from "./types";

type BashResult = { stdout?: string; stderr?: string; exit_code?: number };

function asResult(r: unknown): BashResult {
  if (r && typeof r === "object") return r as BashResult;
  if (typeof r === "string") return { stdout: r };
  return {};
}

export function BashTool({ args, status, result }: ToolRenderProps) {
  const cmd = (args.command as string) ?? "";
  const r = asResult(result);
  const failed = typeof r.exit_code === "number" && r.exit_code !== 0;
  return (
    <details className="chat-tool" data-status={status} data-failed={failed}>
      <summary>
        <code className="chat-tool__cmd">{cmd}</code>
        {typeof r.exit_code === "number" && (
          <span className="chat-tool__badge" data-tone={failed ? "error" : "ok"}>
            exit {r.exit_code}
          </span>
        )}
      </summary>
      {r.stdout && <pre className="chat-tool__code">{r.stdout}</pre>}
      {r.stderr && <pre className="chat-tool__code chat-tool__code--err">{r.stderr}</pre>}
    </details>
  );
}
