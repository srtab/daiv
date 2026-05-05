import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { GitlabTool } from "../tools/gitlab";
import { GhTool } from "../tools/gh";
describe("GitlabTool", () => {
    it("shows command/operation summary", () => {
        render(_jsx(GitlabTool, { name: "gitlab", args: { command: "list_mrs" }, status: "complete", result: "" }));
        expect(screen.getByText(/list_mrs/)).toBeInTheDocument();
    });
});
describe("GhTool", () => {
    it("shows command/operation summary", () => {
        render(_jsx(GhTool, { name: "gh", args: { command: "pr list" }, status: "complete", result: "" }));
        expect(screen.getByText(/pr list/)).toBeInTheDocument();
    });
});
