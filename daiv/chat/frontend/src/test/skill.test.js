import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { SkillTool } from "../tools/skill";
describe("SkillTool", () => {
    it("shows the skill name", () => {
        render(_jsx(SkillTool, { name: "skill", args: { skill: "plan" }, status: "complete", result: "" }));
        expect(screen.getByText(/plan/)).toBeInTheDocument();
    });
});
