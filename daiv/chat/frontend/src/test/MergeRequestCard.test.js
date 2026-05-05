import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { MergeRequestCard } from "../MergeRequestCard";
describe("MergeRequestCard", () => {
    it("renders title and links to web_url", () => {
        render(_jsx(MergeRequestCard, { mr: { merge_request_id: 1, title: "Fix x", web_url: "https://example/mr/1", draft: false } }));
        const link = screen.getByRole("link", { name: /Fix x/ });
        expect(link).toHaveAttribute("href", "https://example/mr/1");
    });
    it("renders nothing when mr is null", () => {
        const { container } = render(_jsx(MergeRequestCard, { mr: null }));
        expect(container).toBeEmptyDOMElement();
    });
});
