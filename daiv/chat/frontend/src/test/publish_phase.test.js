import { jsx as _jsx } from "react/jsx-runtime";
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PublishPhaseChip } from "../tools/publish_phase";
describe("PublishPhaseChip", () => {
    it("renders the right label for PullRequestMetadata", () => {
        render(_jsx(PublishPhaseChip, { name: "PullRequestMetadata", args: {}, status: "running" }));
        expect(screen.getByText(/Creating merge request/)).toBeInTheDocument();
    });
    it("renders the right label for CommitMetadata", () => {
        render(_jsx(PublishPhaseChip, { name: "CommitMetadata", args: {}, status: "complete" }));
        expect(screen.getByText(/Committing/)).toBeInTheDocument();
    });
});
