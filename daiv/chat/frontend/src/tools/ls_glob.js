import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function LsTool({ args, status, result }) {
    const path = args.path ?? "";
    return (_jsxs("details", { className: "chat-tool", "data-status": status, children: [_jsxs("summary", { children: ["ls ", _jsx("span", { className: "chat-tool__path", children: path })] }), _jsx("pre", { className: "chat-tool__code", children: String(result ?? "") })] }));
}
export function GlobTool({ args, status, result }) {
    const pattern = args.pattern ?? "";
    return (_jsxs("details", { className: "chat-tool", "data-status": status, children: [_jsxs("summary", { children: ["glob ", _jsx("code", { children: pattern })] }), _jsx("pre", { className: "chat-tool__code", children: String(result ?? "") })] }));
}
