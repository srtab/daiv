import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { renderTool } from "../tools";

describe("tool registry", () => {
  it("renders fallback when name is unknown", () => {
    render(renderTool({ name: "unknown_tool", args: { x: 1 }, status: "complete", result: "ok" }));
    expect(screen.getByText(/unknown_tool/)).toBeInTheDocument();
  });

  // Each registered name must dispatch to its specific renderer, not silently
  // fall through to FallbackTool — guards against typos in registerTool() calls.
  it.each([
    ["read_file", { file_path: "/a.py" }, /\/a\.py/],
    ["write_file", { file_path: "/b.py", content: "x" }, /\/b\.py/],
    ["edit_file", { file_path: "/c.py", old_str: "a", new_str: "b" }, /\/c\.py/],
    ["grep", { pattern: "needle" }, /needle/],
    ["ls", { path: "/lsdir" }, /\/lsdir/],
    ["glob", { pattern: "*.py" }, /\*\.py/],
    ["bash", { command: "echo hi" }, /echo hi/],
    ["task", { subagent_type: "explorer", description: "look around" }, /explorer/],
    ["skill", { skill: "do_thing" }, /do_thing/],
    ["web_fetch", { url: "https://example.com/x" }, /example\.com\/x/],
    ["web_search", { query: "needle-query" }, /needle-query/],
    ["gitlab", { command: "mr-view" }, /mr-view/],
    ["gh", { command: "pr-view" }, /pr-view/],
  ])("%s dispatches to its specific renderer", (name, args, expected) => {
    const { container } = render(
      renderTool({ name, args, status: "complete", result: "ok" }),
    );
    // FallbackTool renders the bare tool name; specific renderers expose
    // the args. If we fell through to fallback the args would not be visible.
    expect(container.textContent).toMatch(expected);
  });

  it("write_todos dispatches to WriteTodosTool", () => {
    const { container } = render(
      renderTool({
        name: "write_todos",
        args: { todos: [{ content: "ship it", status: "in_progress" }] },
        status: "complete",
      }),
    );
    expect(container.textContent).toMatch(/ship it/);
  });
});
