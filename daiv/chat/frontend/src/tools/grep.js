import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function GrepTool({ args, status, result }) {
    const pattern = args.pattern ?? "";
    const path = args.path ?? "";
    return (_jsxs("details", { className: "chat-tool", "data-status": status, children: [_jsxs("summary", { children: ["grep ", _jsx("code", { children: pattern }), " ", path && _jsx("span", { className: "chat-tool__path", children: path })] }), _jsx("pre", { className: "chat-tool__code", children: String(result ?? "") })] }));
}
