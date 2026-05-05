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
