import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function WebSearchTool({ args, status, result }) {
    const query = args.query ?? "";
    return (_jsxs("details", { className: "chat-tool", "data-status": status, children: [_jsx("summary", { children: _jsx("code", { children: query }) }), result != null && _jsx("pre", { className: "chat-tool__code", children: String(result) })] }));
}
