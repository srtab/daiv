import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export function SkillTool({ args, status }) {
    const skill = args.skill ?? "";
    return (_jsxs("div", { className: "chat-tool chat-tool--skill", "data-status": status, children: [_jsx("span", { className: "chat-tool__badge", "data-tone": "info", children: "skill" }), _jsx("code", { children: skill })] }));
}
