import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ReadFileTool, WriteFileTool, EditFileTool } from "../tools/file_ops";

describe("ReadFileTool", () => {
  it("renders the file path as summary", () => {
    render(<ReadFileTool name="read_file" args={{ file_path: "src/x.py" }} status="complete" result="..." />);
    expect(screen.getByText("src/x.py")).toBeInTheDocument();
  });
});

describe("WriteFileTool", () => {
  it("renders the file path and content preview", () => {
    render(
      <WriteFileTool
        name="write_file"
        args={{ file_path: "src/x.py", content: "print('hi')" }}
        status="complete"
      />,
    );
    expect(screen.getByText("src/x.py")).toBeInTheDocument();
    expect(screen.getByText(/print/)).toBeInTheDocument();
  });
});

describe("EditFileTool", () => {
  it("renders a diff when both old_str and new_str are present", () => {
    render(
      <EditFileTool
        name="edit_file"
        args={{ file_path: "src/x.py", old_str: "x = 1", new_str: "x = 2" }}
        status="complete"
      />,
    );
    expect(screen.getByText("src/x.py")).toBeInTheDocument();
    expect(screen.getByText(/x = 1/)).toBeInTheDocument();
    expect(screen.getByText(/x = 2/)).toBeInTheDocument();
  });
});
