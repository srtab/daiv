import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { TaskTool } from "../tools/task";

describe("TaskTool", () => {
  it("shows the subagent name as summary", () => {
    render(
      <TaskTool
        name="task"
        args={{ subagent_type: "code_review", description: "Review PR #42", prompt: "..." }}
        status="complete"
        result="reviewed"
      />,
    );
    expect(screen.getByText(/code_review/)).toBeInTheDocument();
    expect(screen.getByText(/Review PR/)).toBeInTheDocument();
  });
});
