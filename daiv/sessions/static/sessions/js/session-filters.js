/**
 * Sessions filter bar — client-driven, results-only swap.
 *
 * The filter bar is never inside the swapped region, so it never jumps. It holds
 * filter state initialized FROM THE URL (so first paint and back/forward both stay
 * correct), renders its own active highlights reactively, and swaps only #session-results.
 *
 * History is manual: swapResults() pushState()s the new URL; a single popstate handler
 * re-swaps and tells the bar to re-read the URL. No HTMX-managed history is used.
 */

// One swap path for everything (filters + pagination + popstate).
function swapResults(url, { push = true } = {}) {
    const box = document.getElementById("session-results");
    if (box) box.classList.add("session-results--loading");
    window.htmx
        .ajax("GET", url, { target: "#session-results", swap: "innerHTML" })
        .finally(() => document.getElementById("session-results")?.classList.remove("session-results--loading"));
    if (push) window.history.pushState({}, "", url);
}

// Pagination links inside the results fragment are re-marked on every swap; a delegated
// listener keeps working across swaps without re-binding.
document.addEventListener("click", (event) => {
    const link = event.target.closest("#session-results a[data-page-swap]");
    if (!link) return;
    event.preventDefault();
    swapResults(link.getAttribute("href"));
});

// Back/forward: re-fetch for the popped URL (no extra push) and re-sync the bar.
window.addEventListener("popstate", () => {
    swapResults(window.location.pathname + window.location.search, { push: false });
    window.dispatchEvent(new CustomEvent("sessions:url-changed"));
});

document.addEventListener("alpine:init", () => {
    Alpine.data("filterBar", () => ({
        open: null, // id of the open dropdown, or null
        q: "",
        status: "",
        trigger: "",
        triggerLabel: "",
        range: "",
        rangeLabel: "",
        from: "",
        to: "",
        repo: "",
        schedule: "",
        batch: "",

        init() {
            this._readUrl();
            // Re-sync on back/forward (popstate handler dispatches this).
            window.addEventListener("sessions:url-changed", () => this._readUrl());
        },

        _readUrl() {
            const p = new URLSearchParams(window.location.search);
            this.q = p.get("q") || "";
            this.status = p.get("status") || "";
            this.trigger = p.get("trigger") || "";
            this.range = p.get("range") || "";
            this.from = p.get("date_from") || "";
            this.to = p.get("date_to") || "";
            this.repo = p.get("repo") || "";
            this.schedule = p.get("schedule") || "";
            this.batch = p.get("batch") || "";
            // Labels for the Type/Time buttons come from the rendered menu items
            // (single source of truth = the template), keyed by value.
            this.triggerLabel = this._labelFor("trigger", this.trigger);
            this.rangeLabel = this._labelFor("range", this.range);
        },

        _labelFor(kind, value) {
            if (!value) return "";
            const el = this.$root.querySelector(`[data-${kind}-value="${value}"]`);
            return el ? el.dataset.label || el.textContent.trim() : "";
        },

        _apply() {
            const params = new URLSearchParams();
            const set = (k, v) => {
                if (v) params.set(k, v);
            };
            set("q", this.q);
            set("status", this.status);
            set("trigger", this.trigger);
            set("range", this.range);
            set("date_from", this.from);
            set("date_to", this.to);
            set("repo", this.repo);
            set("schedule", this.schedule);
            set("batch", this.batch);
            const qs = params.toString();
            swapResults(window.location.pathname + (qs ? "?" + qs : ""));
            this.open = null;
        },

        toggle(id) {
            this.open = this.open === id ? null : id;
        },

        setStatus(value) {
            this.status = value;
            this._apply();
        },
        setTrigger(value, label) {
            this.trigger = value;
            this.triggerLabel = label;
            this._apply();
        },
        clearTrigger() {
            this.trigger = "";
            this.triggerLabel = "";
            this._apply();
        },
        setRange(value, label) {
            this.range = value;
            this.rangeLabel = label;
            this.from = "";
            this.to = "";
            this._apply();
        },
        setCustomRange(from, to) {
            this.range = "";
            this.rangeLabel = "";
            this.from = from;
            this.to = to;
            this._apply();
        },
        clearTime() {
            this.range = "";
            this.rangeLabel = "";
            this.from = "";
            this.to = "";
            this._apply();
        },
        clearParam(key) {
            this[key] = "";
            this._apply();
        },
        clearAll() {
            this.q = "";
            this.status = "";
            this.trigger = "";
            this.triggerLabel = "";
            this.range = "";
            this.rangeLabel = "";
            this.from = "";
            this.to = "";
            this.repo = "";
            this.schedule = "";
            this.batch = "";
            this._apply();
        },

        get timeLabel() {
            if (this.rangeLabel) return this.rangeLabel;
            if (this.from || this.to) return (this.from || "…") + " – " + (this.to || "…");
            return "";
        },
        get timeActive() {
            return !!(this.range || this.from || this.to);
        },
        get hasActiveFilters() {
            return !!(
                this.q ||
                this.status ||
                this.trigger ||
                this.range ||
                this.from ||
                this.to ||
                this.repo ||
                this.schedule ||
                this.batch
            );
        },
    }));
});
