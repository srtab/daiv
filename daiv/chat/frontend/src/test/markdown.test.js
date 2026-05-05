import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Markdown } from "../markdown";
describe("Markdown", () => {
    it("renders bold and links", () => {
        render(_jsx(Markdown, { source: "**hi** [there](https://example)" }));
        expect(screen.getByText("hi").tagName).toBe("STRONG");
        expect(screen.getByRole("link")).toHaveAttribute("href", "https://example");
    });
    it("renders fenced code with syntax highlight wrapper", () => {
        render(_jsx(Markdown, { source: "```python\nprint('hi')\n```" }));
        expect(screen.getByText(/print/)).toBeInTheDocument();
    });
});
