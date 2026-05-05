import type React from "react";
import { parseDiff, Diff, Hunk } from "react-diff-view";
import "react-diff-view/style/index.css";
import type { ToolRenderProps } from "./types";

const card = (status: string, summary: React.ReactNode, body?: React.ReactNode) => (
  <details className="chat-tool" data-status={status} open={status === "complete"}>
    <summary>{summary}</summary>
    {body ? <div className="chat-tool__body">{body}</div> : null}
  </details>
);

export function ReadFileTool({ args, status, result }: ToolRenderProps) {
  const path = (args.file_path as string) ?? "";
  return card(status, path || "read_file", <pre className="chat-tool__code">{String(result ?? "")}</pre>);
}

export function WriteFileTool({ args, status }: ToolRenderProps) {
  const path = (args.file_path as string) ?? "";
  const content = (args.content as string) ?? "";
  return card(status, path || "write_file", <pre className="chat-tool__code">{content}</pre>);
}

export function EditFileTool({ args, status }: ToolRenderProps) {
  const path = (args.file_path as string) ?? "";
  const oldStr = (args.old_str as string) ?? "";
  const newStr = (args.new_str as string) ?? "";
  if (!oldStr || !newStr) return card(status, path || "edit_file");
  let file: ReturnType<typeof parseDiff>[number] | undefined;
  try {
    [file] = parseDiff(makeUnifiedDiff(path, oldStr, newStr), { nearbySequences: "zip" });
  } catch (err) {
    console.warn("EditFileTool: parseDiff failed", err);
  }
  return card(
    status,
    path || "edit_file",
    file ? (
      <Diff viewType="unified" diffType="modify" hunks={file.hunks}>
        {(hunks) => hunks.map((h) => <Hunk key={h.content} hunk={h} />)}
      </Diff>
    ) : (
      <pre>{newStr}</pre>
    ),
  );
}

function makeUnifiedDiff(path: string, before: string, after: string): string {
  const beforeLines = before.split("\n");
  const afterLines = after.split("\n");
  return [
    `--- a/${path}`,
    `+++ b/${path}`,
    `@@ -1,${beforeLines.length} +1,${afterLines.length} @@`,
    ...beforeLines.map((l) => `-${l}`),
    ...afterLines.map((l) => `+${l}`),
  ].join("\n");
}
