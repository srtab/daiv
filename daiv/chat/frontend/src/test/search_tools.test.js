import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { GrepTool } from "../tools/grep";
import { LsTool, GlobTool } from "../tools/ls_glob";
describe("GrepTool", () => {
    it("shows pattern and result count", () => {
        render(_jsx(GrepTool, { name: "grep", args: { pattern: "TODO", path: "daiv/" }, status: "complete", result: "3 matches" }));
        expect(screen.getByText(/TODO/)).toBeInTheDocument();
    });
});
describe("LsTool", () => {
    it("shows the path", () => {
        render(_jsx(LsTool, { name: "ls", args: { path: "daiv/" }, status: "complete", result: "a\\nb\\nc" }));
        expect(screen.getByText("daiv/")).toBeInTheDocument();
    });
});
describe("GlobTool", () => {
    it("shows the pattern", () => {
        render(_jsx(GlobTool, { name: "glob", args: { pattern: "**/*.py" }, status: "complete", result: "" }));
        expect(screen.getByText("**/*.py")).toBeInTheDocument();
    });
});
