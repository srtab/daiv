/** Artifacts list filter bar; swap core lives in core/js/results-swap.js. */

const { swapResults } = window.createResultsSwap("artifact-results", {
    errorMessage: "Couldn't update the artifacts list — check your connection and try again.",
    onUrlChanged: () => window.dispatchEvent(new CustomEvent("artifacts:url-changed")),
});

document.addEventListener("alpine:init", () => {
    const FIELDS = ["q", "kind", "repo"];

    Alpine.data("artifactFilters", () => ({
        open: null,
        q: "",
        kind: "",
        repo: "",

        init() {
            this._readUrl();
            window.addEventListener("artifacts:url-changed", () => this._readUrl());
        },

        _readUrl() {
            const p = new URLSearchParams(window.location.search);
            for (const field of FIELDS) this[field] = p.get(field) || "";
        },

        _apply() {
            const params = new URLSearchParams();
            for (const field of FIELDS) if (this[field]) params.set(field, this[field]);
            const qs = params.toString();
            swapResults(window.location.pathname + (qs ? "?" + qs : ""));
            this.open = null;
        },

        // The contract sessions/_filter_dropdown.html binds to.
        toggle(key) {
            this.open = this.open === key ? null : key;
        },
        value(key) {
            return this[key];
        },
        pick(key, value) {
            this[key] = value;
            this._apply();
        },
        labelFor(key) {
            const value = this.value(key);
            if (!value) return "";
            const el = this.$root.querySelector(`[data-filter-key="${key}"][data-value="${CSS.escape(value)}"]`);
            return el ? el.dataset.label : value;
        },
        clearAll() {
            for (const field of FIELDS) this[field] = "";
            this._apply();
        },
    }));
});
