import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function WebFetchTool({ args, status, result }) {
    const url = args.url ?? "";
    return (_jsxs("details", { className: "chat-tool", "data-status": status, children: [_jsx("summary", { children: _jsx("a", { href: url, children: url }) }), result != null && _jsx("pre", { className: "chat-tool__code", children: String(result) })] }));
}
