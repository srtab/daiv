import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
function asResult(r) {
    if (r && typeof r === "object")
        return r;
    if (typeof r === "string")
        return { stdout: r };
    return {};
}
export function BashTool({ args, status, result }) {
    const cmd = args.command ?? "";
    const r = asResult(result);
    const failed = typeof r.exit_code === "number" && r.exit_code !== 0;
    return (_jsxs("details", { className: "chat-tool", "data-status": status, "data-failed": failed, children: [_jsxs("summary", { children: [_jsx("code", { className: "chat-tool__cmd", children: cmd }), typeof r.exit_code === "number" && (_jsxs("span", { className: "chat-tool__badge", "data-tone": failed ? "error" : "ok", children: ["exit ", r.exit_code] }))] }), r.stdout && _jsx("pre", { className: "chat-tool__code", children: r.stdout }), r.stderr && _jsx("pre", { className: "chat-tool__code chat-tool__code--err", children: r.stderr })] }));
}
