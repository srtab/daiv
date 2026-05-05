import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { parseDiff, Diff, Hunk } from "react-diff-view";
import "react-diff-view/style/index.css";
const card = (status, summary, body) => (_jsxs("details", { className: "chat-tool", "data-status": status, open: status === "complete", children: [_jsx("summary", { children: summary }), body ? _jsx("div", { className: "chat-tool__body", children: body }) : null] }));
export function ReadFileTool({ args, status, result }) {
    const path = args.file_path ?? "";
    return card(status, path || "read_file", _jsx("pre", { className: "chat-tool__code", children: String(result ?? "") }));
}
export function WriteFileTool({ args, status }) {
    const path = args.file_path ?? "";
    const content = args.content ?? "";
    return card(status, path || "write_file", _jsx("pre", { className: "chat-tool__code", children: content }));
}
export function EditFileTool({ args, status }) {
    const path = args.file_path ?? "";
    const oldStr = args.old_str ?? "";
    const newStr = args.new_str ?? "";
    if (!oldStr || !newStr)
        return card(status, path || "edit_file");
    const unified = makeUnifiedDiff(path, oldStr, newStr);
    const [file] = parseDiff(unified, { nearbySequences: "zip" });
    return card(status, path || "edit_file", file ? (_jsx(Diff, { viewType: "unified", diffType: "modify", hunks: file.hunks, children: (hunks) => hunks.map((h) => _jsx(Hunk, { hunk: h }, h.content)) })) : (_jsx("pre", { children: newStr })));
}
function makeUnifiedDiff(path, before, after) {
    const beforeLines = before.split("\n");
    const afterLines = after.split("\n");
    return [
        `--- a/${path}`,
        `+++ b/${path}`,
        `@@ -1,${beforeLines.length} +1,${afterLines.length} @@`,
        ...beforeLines.map((l) => `-${l}`),
        ...afterLines.map((l) => `+${l}`),
    ].join("\n");
}
