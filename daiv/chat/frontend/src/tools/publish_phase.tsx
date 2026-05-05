import type { ToolRenderProps } from "./types";

export const PUBLISH_PHASE_LABELS: Record<string, string> = {
  PullRequestMetadata: "Creating merge request",
  CommitMetadata: "Committing changes",
};

export function PublishPhaseChip({ name, status }: ToolRenderProps) {
  const label = PUBLISH_PHASE_LABELS[name] ?? name;
  return (
    <div className="chat-phase" data-status={status}>
      <span className="chat-phase__icon" aria-hidden>
        {status === "running" ? (
          <span className="chat-phase__spinner" />
        ) : status === "complete" ? (
          "✓"
        ) : (
          "!"
        )}
      </span>
      <span className="chat-phase__label">{label}</span>
      {status === "running" && <span className="chat-phase__suffix">…</span>}
    </div>
  );
}
