import { useState, useEffect } from "react";
import { CopilotKit, useDefaultTool } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import "@copilotkit/react-ui/styles.css";
import { HttpAgent } from "@ag-ui/client";
import type { MountConfig } from "./config";
import { MergeRequestCard } from "./MergeRequestCard";
import { useDaivState } from "./state/coagent";
import { useRunStatus } from "./use_run_status";
import { renderTool } from "./tools";
import type { ToolStatus } from "./tools/types";

function mapStatus(s: string): ToolStatus {
  if (s === "complete") return "complete";
  return "running";
}

function ChatBody({ threadId }: { threadId: string }) {
  const { state } = useDaivState();
  const [blocked, setBlocked] = useState(false);
  const { active } = useRunStatus(`/api/chat/threads/${threadId}/status`);

  useDefaultTool({
    render: ({ name, args, status, result }) =>
      renderTool({
        name,
        args: (args ?? {}) as Record<string, unknown>,
        status: mapStatus(status),
        result,
      }),
  });

  useEffect(() => {
    if (blocked && !active) setBlocked(false);
  }, [blocked, active]);

  return (
    <div className="chat-layout">
      {blocked && <div className="chat-blocked-banner">Another run is in progress…</div>}
      <CopilotChat />
      <MergeRequestCard mr={state.merge_request ?? null} />
    </div>
  );
}

export function Chat({ cfg }: { cfg: MountConfig }) {
  return (
    <CopilotKit
      selfManagedAgents={{
        DAIV: new HttpAgent({
          url: "/api/chat/completions",
          headers: {
            "X-Repo-ID": cfg.repoId,
            "X-Ref": cfg.ref,
            "X-CSRFToken": cfg.csrf,
          },
          threadId: cfg.threadId,
        }),
      }}
    >
      <ChatBody threadId={cfg.threadId} />
    </CopilotKit>
  );
}
