import type { ToolRenderProps } from "./types";

type Todo = { content: string; status: "pending" | "in_progress" | "completed" };

const ICON: Record<Todo["status"], string> = { pending: "○", in_progress: "◐", completed: "●" };
const warned = new Set<string>();

function iconFor(s: Todo["status"]): string {
  const i = ICON[s];
  if (i !== undefined) return i;
  if (!warned.has(s)) {
    warned.add(s);
    console.warn(`WriteTodosTool: unknown todo status ${JSON.stringify(s)}`);
  }
  return "?";
}

export function WriteTodosTool({ args, status }: ToolRenderProps) {
  const todos = (args.todos as Todo[] | undefined) ?? [];
  return (
    <div className="chat-todos" data-status={status}>
      <ul>
        {todos.map((t, i) => (
          <li key={i} data-todo-status={t.status}>
            <span className="chat-todos__icon">{iconFor(t.status)}</span>
            <span className="chat-todos__content">{t.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
