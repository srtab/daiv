/**
 * Sessions filter bar — client-driven, results-only swap.
 *
 * The swap core lives in core/js/results-swap.js; this file owns only the Alpine
 * filterBar() component, which holds filter state initialized FROM THE URL and swaps
 * only #session-results via the shared swapResults().
 */

const SPEND_PARAMS = [
    "spend_from",
    "spend_to",
    "spend_person",
    "spend_repo",
    "spend_trigger",
    "spend_task",
    "spend_model",
    "spend_provider",
];
// The Spend page's params with no control in the bar: they show as one banner.
const SPEND_BANNER_PARAMS = ["spend_from", "spend_to", "spend_repo", "spend_trigger", "spend_task"];

const { swapResults } = window.createResultsSwap("session-results", {
    errorMessage: "Couldn't update the session list — check your connection and try again.",
    onUrlChanged: () => window.dispatchEvent(new CustomEvent("sessions:url-changed")),
});

document.addEventListener("alpine:init", () => {
    Alpine.data("filterBar", () => ({
        open: null, // id of the open dropdown, or null
        scope: "mine",
        q: "",
        status: "",
        trigger: "",
        range: "",
        from: "",
        to: "",
        repo: "",
        mr: "",
        ranOn: "",
        schedule: "",
        batch: "",
        archived: "",
        sort: "",
        // The Spend page's drill-down params, keyed by their URL names.
        spend: Object.fromEntries(SPEND_PARAMS.map((name) => [name, ""])),

        init() {
            this._readUrl();
            // Re-sync on back/forward (popstate handler dispatches this).
            window.addEventListener("sessions:url-changed", () => this._readUrl());
        },

        _readUrl() {
            const p = new URLSearchParams(window.location.search);
            // Only the server-rendered URL can lack a scope (_apply always sets one), so take the scope it resolved.
            this.scope = p.get("scope") || this.$root.dataset.scope;
            this.q = p.get("q") || "";
            this.status = p.get("status") || "";
            this.trigger = p.get("trigger") || "";
            this.range = p.get("range") || "";
            this.from = p.get("date_from") || "";
            this.to = p.get("date_to") || "";
            this.repo = p.get("repo") || "";
            this.mr = p.get("mr") || "";
            this.ranOn = p.get("ran_on") || "";
            this.schedule = p.get("schedule") || "";
            this.batch = p.get("batch") || "";
            const archived = p.get("archived");
            this.archived = archived === "true" || archived === "all" ? archived : "";
            this.sort = p.get("sort") || this.$root.dataset.defaultSort;
            SPEND_PARAMS.forEach((name) => (this.spend[name] = p.get(name) || ""));
        },

        _apply() {
            const params = new URLSearchParams();
            const set = (k, v) => {
                if (v) params.set(k, v);
            };
            // Always explicit: clearing a deep-link chip must not silently flip "all" back to "mine".
            set("scope", this.scope);
            set("q", this.q);
            set("status", this.status);
            set("trigger", this.trigger);
            set("range", this.range);
            set("date_from", this.from);
            set("date_to", this.to);
            set("repo", this.repo);
            set("mr", this.mr);
            set("ran_on", this.ranOn);
            set("schedule", this.schedule);
            set("batch", this.batch);
            set("archived", this.archived);
            SPEND_PARAMS.forEach((name) => set(name, this.spend[name]));
            set("sort", this.sort === this.$root.dataset.defaultSort ? "" : this.sort);
            const qs = params.toString();
            swapResults(window.location.pathname + (qs ? "?" + qs : ""));
            this.open = null;
        },

        setScope(value) {
            this.scope = value;
            this._apply();
        },
        ...window.filterDropdown(),
        _slot(key) {
            return key.startsWith("spend_") ? this.spend : this;
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
        toggleArchived() {
            this.archived = this.archived === "true" ? "" : "true";
            this._apply();
        },
        clearParam(...keys) {
            keys.forEach((key) => (this[key] = ""));
            this._apply();
        },
        clearSpend(...names) {
            names.forEach((name) => (this.spend[name] = ""));
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
            this.ranOn = "";
            this.schedule = "";
            this.batch = "";
            this.archived = "";
            SPEND_PARAMS.forEach((name) => (this.spend[name] = ""));
            this._apply();
        },

        get timeLabel() {
            if (this.range) return this.labelFor("range");
            if (this.from || this.to) return (this.from || "…") + " – " + (this.to || "…");
            return "";
        },
        get timeActive() {
            return !!(this.range || this.from || this.to);
        },
        get hasChips() {
            return !!(this.repo || this.mr || this.archived === "all" || this.ranOn || this.schedule || this.batch);
        },
        get hasSpendBanner() {
            return SPEND_BANNER_PARAMS.some((name) => this.spend[name]);
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
                this.ranOn ||
                this.schedule ||
                this.batch ||
                this.archived ||
                SPEND_PARAMS.some((name) => this.spend[name])
            );
        },
    }));
});
