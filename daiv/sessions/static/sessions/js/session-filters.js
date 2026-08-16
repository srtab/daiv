/**
 * Sessions filter bar — client-driven, results-only swap.
 *
 * The swap core lives in core/js/results-swap.js; this file owns only the Alpine
 * filterBar() component, which holds filter state initialized FROM THE URL and swaps
 * only #session-results via the shared swapResults().
 */

const { swapResults } = window.createResultsSwap("session-results", {
    errorMessage: "Couldn't update the session list — check your connection and try again.",
    onUrlChanged: () => window.dispatchEvent(new CustomEvent("sessions:url-changed")),
});

document.addEventListener("alpine:init", () => {
    Alpine.data("filterBar", () => ({
        open: null, // id of the open dropdown, or null
        q: "",
        status: "",
        trigger: "",
        range: "",
        from: "",
        to: "",
        repo: "",
        mr: "",
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
            this.mr = p.get("mr") || "";
            this.schedule = p.get("schedule") || "";
            this.batch = p.get("batch") || "";
        },

        _labelFor(kind, value) {
            if (!value) return "";
            const el = this.$root.querySelector(`[data-${kind}-value="${value}"]`);
            if (el) return el.dataset.label || el.textContent.trim();
            // A URL value with no matching menu item (renamed/removed enum, hand-edited
            // URL): show the raw value so the active filter stays visible, and log it.
            console.warn(`filterBar: no menu item for ${kind}="${value}"; showing raw value`);
            return value;
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
            set("mr", this.mr);
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
        setTrigger(value) {
            this.trigger = value;
            this._apply();
        },
        clearTrigger() {
            this.trigger = "";
            this._apply();
        },
        setRange(value) {
            this.range = value;
            this.from = "";
            this.to = "";
            this._apply();
        },
        setCustomRange(from, to) {
            this.range = "";
            this.from = from;
            this.to = to;
            this._apply();
        },
        clearTime() {
            this.range = "";
            this.from = "";
            this.to = "";
            this._apply();
        },
        clearParam(...keys) {
            keys.forEach((key) => (this[key] = ""));
            this._apply();
        },
        clearAll() {
            this.q = "";
            this.status = "";
            this.trigger = "";
            this.range = "";
            this.from = "";
            this.to = "";
            this.repo = "";
            this.mr = "";
            this.schedule = "";
            this.batch = "";
            this._apply();
        },

        // Button labels are derived from the rendered menu items (single source of truth =
        // the template), so they never need to be stored or cleared alongside the value.
        get triggerLabel() {
            return this._labelFor("trigger", this.trigger);
        },
        get rangeLabel() {
            return this._labelFor("range", this.range);
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
                this.mr ||
                this.schedule ||
                this.batch
            );
        },
    }));
});
