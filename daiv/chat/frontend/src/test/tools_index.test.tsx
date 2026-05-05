import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { renderTool } from "../tools";

describe("tool registry", () => {
  it("renders fallback when name is unknown", () => {
    render(renderTool({ name: "unknown_tool", args: { x: 1 }, status: "complete", result: "ok" }));
    expect(screen.getByText(/unknown_tool/)).toBeInTheDocument();
  });
});
