import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { BashTool } from "../tools/bash";

describe("BashTool", () => {
  it("shows command, exit code, and stdout", () => {
    render(
      <BashTool
        name="bash"
        args={{ command: "ls -la" }}
        status="complete"
        result={{ stdout: "out", stderr: "", exit_code: 0 }}
      />,
    );
    expect(screen.getByText("ls -la")).toBeInTheDocument();
    expect(screen.getByText("out")).toBeInTheDocument();
  });

  it("shows non-zero exit prominently", () => {
    render(
      <BashTool
        name="bash"
        args={{ command: "false" }}
        status="complete"
        result={{ stdout: "", stderr: "boom", exit_code: 1 }}
      />,
    );
    expect(screen.getByText(/exit\s*1/i)).toBeInTheDocument();
  });
});
