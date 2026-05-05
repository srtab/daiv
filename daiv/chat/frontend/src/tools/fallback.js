import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function FallbackTool({ name, args, result, status }) {
    return (_jsxs("details", { className: "chat-tool chat-tool--fallback", "data-status": status, children: [_jsx("summary", { children: name }), _jsx("pre", { children: JSON.stringify({ args, result }, null, 2) })] }));
}
