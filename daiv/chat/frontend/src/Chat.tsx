import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import "@copilotkit/react-ui/styles.css";
import { HttpAgent } from "@ag-ui/client";
import type { MountConfig } from "./config";

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
      <CopilotChat />
    </CopilotKit>
  );
}
