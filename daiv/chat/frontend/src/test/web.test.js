import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { WebFetchTool } from "../tools/web_fetch";
import { WebSearchTool } from "../tools/web_search";
describe("WebFetchTool", () => {
    it("renders the URL as link", () => {
        render(_jsx(WebFetchTool, { name: "web_fetch", args: { url: "https://example/" }, status: "complete", result: "" }));
        expect(screen.getByRole("link")).toHaveAttribute("href", "https://example/");
    });
});
describe("WebSearchTool", () => {
    it("renders the query", () => {
        render(_jsx(WebSearchTool, { name: "web_search", args: { query: "rust async" }, status: "complete", result: "" }));
        expect(screen.getByText(/rust async/)).toBeInTheDocument();
    });
});
