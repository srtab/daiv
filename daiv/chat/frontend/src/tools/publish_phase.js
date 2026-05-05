import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
export const PUBLISH_PHASE_LABELS = {
    PullRequestMetadata: "Creating merge request",
    CommitMetadata: "Committing changes",
};
export function PublishPhaseChip({ name, status }) {
    const label = PUBLISH_PHASE_LABELS[name] ?? name;
    return (_jsxs("div", { className: "chat-phase", "data-status": status, children: [_jsx("span", { className: "chat-phase__icon", "aria-hidden": true, children: status === "running" ? (_jsx("span", { className: "chat-phase__spinner" })) : status === "complete" ? ("✓") : ("!") }), _jsx("span", { className: "chat-phase__label", children: label }), status === "running" && _jsx("span", { className: "chat-phase__suffix", children: "\u2026" })] }));
}
