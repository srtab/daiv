import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState, useEffect } from "react";
import { CopilotKit, useDefaultTool } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import "@copilotkit/react-ui/styles.css";
import { HttpAgent } from "@ag-ui/client";
import { MergeRequestCard } from "./MergeRequestCard";
import { useDaivState } from "./state/coagent";
import { useRunStatus } from "./use_run_status";
import { renderTool } from "./tools";
function mapStatus(s) {
    if (s === "complete")
        return "complete";
    return "running";
}
function ChatBody({ threadId }) {
    const { state } = useDaivState();
    const [blocked, setBlocked] = useState(false);
    const { active } = useRunStatus(`/api/chat/threads/${threadId}/status`);
    useDefaultTool({
        render: ({ name, args, status, result }) => renderTool({
            name,
            args: (args ?? {}),
            status: mapStatus(status),
            result,
        }),
    });
    useEffect(() => {
        if (blocked && !active)
            setBlocked(false);
    }, [blocked, active]);
    return (_jsxs("div", { className: "chat-layout", children: [blocked && _jsx("div", { className: "chat-blocked-banner", children: "Another run is in progress\u2026" }), _jsx(CopilotChat, {}), _jsx(MergeRequestCard, { mr: state.merge_request ?? null })] }));
}
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
        }, children: _jsx(ChatBody, { threadId: cfg.threadId }) }));
}
