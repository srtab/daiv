/**
 * Memory list filter bar — client-driven, results-only swap.
 *
 * Uses the shared swap core (core/js/results-swap.js). The bar holds q + status read
 * from the URL and swaps only #memory-results.
 */

const { swapResults } = window.createResultsSwap("memory-results", {
    errorMessage: "Couldn't update the memory list — check your connection and try again.",
    onUrlChanged: () => window.dispatchEvent(new CustomEvent("memory:url-changed")),
});

document.addEventListener("alpine:init", () => {
    Alpine.data("memoryFilters", () => ({
        q: "",
        status: "",

        init() {
            this._readUrl();
            window.addEventListener("memory:url-changed", () => this._readUrl());
        },

        _readUrl() {
            const p = new URLSearchParams(window.location.search);
            this.q = p.get("q") || "";
            this.status = p.get("status") || "";
        },

        _apply() {
            const params = new URLSearchParams();
            if (this.q) params.set("q", this.q);
            if (this.status) params.set("status", this.status);
            const qs = params.toString();
            swapResults(window.location.pathname + (qs ? "?" + qs : ""));
        },

        setStatus(value) {
            this.status = value;
            this._apply();
        },
    }));
});
