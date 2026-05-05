import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
const ICON = { pending: "○", in_progress: "◐", completed: "●" };
export function WriteTodosTool({ args, status }) {
    const todos = args.todos ?? [];
    return (_jsx("div", { className: "chat-todos", "data-status": status, children: _jsx("ul", { children: todos.map((t, i) => (_jsxs("li", { "data-todo-status": t.status, children: [_jsx("span", { className: "chat-todos__icon", children: ICON[t.status] ?? "?" }), _jsx("span", { className: "chat-todos__content", children: t.content })] }, i))) }) }));
}
