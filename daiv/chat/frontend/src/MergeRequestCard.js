import { jsx as _jsx } from "react/jsx-runtime";
export function MergeRequestCard({ mr }) {
    if (!mr)
        return null;
    return (_jsx("aside", { className: "chat-mr-card", "data-state": mr.draft ? "draft" : "opened", children: _jsx("a", { href: mr.web_url, children: mr.title }) }));
}
