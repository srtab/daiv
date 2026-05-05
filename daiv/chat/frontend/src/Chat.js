import { jsx as _jsx } from "react/jsx-runtime";
import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import "@copilotkit/react-ui/styles.css";
import { HttpAgent } from "@ag-ui/client";
export function Chat({ cfg }) {
    return (_jsx(CopilotKit, { selfManagedAgents: {
            DAIV: new HttpAgent({
                url: "/api/chat/completions",
                headers: {
                    "X-Repo-ID": cfg.repoId,
                    "X-Ref": cfg.ref,
                    "X-CSRFToken": cfg.csrf,
                },
                threadId: cfg.threadId,
            }),
        }, children: _jsx(CopilotChat, {}) }));
}
