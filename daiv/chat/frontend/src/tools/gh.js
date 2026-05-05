import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function GhTool({ args, status, result }) {
    const command = args.command ?? args.subcommand ?? "";
    return (_jsxs("details", { className: "chat-tool", "data-status": status, children: [_jsxs("summary", { children: ["gh ", _jsx("code", { children: command })] }), result != null && _jsx("pre", { className: "chat-tool__code", children: String(result) })] }));
}
