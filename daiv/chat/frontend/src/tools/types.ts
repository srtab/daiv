import type { ReactElement } from "react";

export type ToolStatus = "running" | "complete" | "error";

export type ToolRenderProps = {
  name: string;
  args: Record<string, unknown>;
  argsRaw?: string;
  result?: unknown;
  status: ToolStatus;
};

export type ToolRenderer = (props: ToolRenderProps) => ReactElement;

export function formatResult(r: unknown): string {
  if (r == null) return "";
  if (typeof r === "string") return r;
  if (typeof r === "number" || typeof r === "boolean") return String(r);
  try {
    return JSON.stringify(r, null, 2);
  } catch {
    return String(r);
  }
}
