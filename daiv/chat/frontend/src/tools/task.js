import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function TaskTool({ args, status, result }) {
    const sub = args.subagent_type ?? "subagent";
    const desc = args.description ?? "";
    return (_jsxs("details", { className: "chat-tool chat-tool--task", "data-status": status, children: [_jsxs("summary", { children: [_jsx("span", { className: "chat-tool__badge", "data-tone": "info", children: sub }), desc && _jsx("span", { className: "chat-tool__path", children: desc })] }), result != null && _jsx("pre", { className: "chat-tool__code", children: String(result) })] }));
}
