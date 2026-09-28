/** Artifacts list filter bar; swap core lives in core/js/results-swap.js. */

const { swapResults } = window.createResultsSwap("artifact-results", {
    errorMessage: "Couldn't update the artifacts list — check your connection and try again.",
    onUrlChanged: () => window.dispatchEvent(new CustomEvent("artifacts:url-changed")),
});

document.addEventListener("alpine:init", () => {
    Alpine.data("artifactFilters", () => ({
        q: "",
        kind: "",
        repo: "",

        init() {
            this._readUrl();
            window.addEventListener("artifacts:url-changed", () => this._readUrl());
        },

        _readUrl() {
            const p = new URLSearchParams(window.location.search);
            this.q = p.get("q") || "";
            this.kind = p.get("kind") || "";
            this.repo = p.get("repo") || "";
        },

        _apply() {
            const params = new URLSearchParams();
            if (this.q) params.set("q", this.q);
            if (this.kind) params.set("kind", this.kind);
            if (this.repo) params.set("repo", this.repo);
            const qs = params.toString();
            swapResults(window.location.pathname + (qs ? "?" + qs : ""));
        },

        setKind(value) {
            this.kind = value;
            this._apply();
        },
    }));
});
