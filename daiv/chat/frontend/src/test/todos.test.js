import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { WriteTodosTool } from "../tools/todos";
describe("WriteTodosTool", () => {
    it("renders one row per todo with status indicator", () => {
        render(_jsx(WriteTodosTool, { name: "write_todos", args: {
                todos: [
                    { content: "first", status: "completed" },
                    { content: "second", status: "pending" },
                ],
            }, status: "complete" }));
        expect(screen.getByText("first")).toBeInTheDocument();
        expect(screen.getByText("second")).toBeInTheDocument();
        expect(screen.getAllByRole("listitem")).toHaveLength(2);
    });
});
