/**
 * Sessions filter bar — client-driven, results-only swap.
 *
 * The swap core lives in core/js/results-swap.js; this file owns only the Alpine
 * filterBar() component, which holds filter state initialized FROM THE URL and swaps
 * only #session-results via the shared swapResults().
 *
 * The Filter menu (sessions/_filter_menu.html) lists the filters by name and drills into one
 * (`property`) in place; a filter whose values come from the server loads them on that first
 * drill, again whenever its search or a param in its `data-reload-on` changes. Applied filters show
 * as chips (sessions/_filter_chip.html).
 */

// The Spend page's params: those with no control in the bar show as one banner.
const SPEND_BANNER_PARAMS = ["spend_from", "spend_to", "spend_repo", "spend_trigger", "spend_task"];
const SPEND_PARAMS = [...SPEND_BANNER_PARAMS, "spend_person", "spend_model", "spend_provider"];
const CHIP_KEYS = ["status", "trigger", "repo", "mr", "spend_person", "spend_model", "spend_provider", "schedule", "batch", "ranOn"];
const TYPING = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';
const MENU_WIDTH = 288;
const DISPLAY_WIDTH = 320;

const { swapResults } = window.createResultsSwap("session-results", {
    errorMessage: "Couldn't update the session list — check your connection and try again.",
    onUrlChanged: () => window.dispatchEvent(new CustomEvent("sessions:url-changed")),
});

document.addEventListener("alpine:init", () => {
    Alpine.data("filterBar", () => ({
        open: null, // id of the open menu, or null
        property: null, // the filter the Filter menu has drilled into, or null for the list of filters
        propertyQuery: "",
        valueQuery: "",
        anchor: null, // where the Filter menu opens, relative to the bar
        displayLeft: null, // where the Display menu opens, relative to its button
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
        group: "",
        _clears: {},
        _refreshTimer: null,
        ...Object.fromEntries(SPEND_PARAMS.map((name) => [name, ""])),

        init() {
            this._clears = JSON.parse(this.$root.dataset.clears || "{}");
            this._readUrl();
            // Re-sync on back/forward (popstate handler dispatches this).
            window.addEventListener("sessions:url-changed", () => this._readUrl());
            // Grouped by status, a row whose status changed belongs under another header.
            window.addEventListener("sessions:status-changed", () => {
                if (this.group === "status") this._refresh();
            });
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
            this.group = p.get("group") || "";
            SPEND_PARAMS.forEach((name) => (this[name] = p.get(name) || ""));
        },

        _params() {
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
            SPEND_PARAMS.forEach((name) => set(name, this[name]));
            set("sort", this.sort === this.$root.dataset.defaultSort ? "" : this.sort);
            set("group", this.group);
            return params;
        },

        _apply() {
            const qs = this._params().toString();
            swapResults(window.location.pathname + (qs ? "?" + qs : ""));
            this.open = null;
            this.property = null;
        },

        setScope(value) {
            this.scope = value;
            this._apply();
        },
        setGroup(value) {
            this.group = value;
            this._apply();
        },
        _refresh() {
            clearTimeout(this._refreshTimer);
            this._refreshTimer = setTimeout(() => {
                // A swap in flight has not pushed its URL yet; refreshing now would undo it.
                if (document.getElementById("session-results")?.classList.contains("results--loading")) {
                    this._refresh();
                    return;
                }
                const params = this._params();
                const page = new URLSearchParams(window.location.search).get("page");
                if (page) params.set("page", page);
                swapResults(window.location.pathname + "?" + params.toString(), { push: false, quiet: true });
            }, 1000);
        },
        toggleDisplay(wrapper) {
            if (this.open === "display") {
                this.open = null;
                return;
            }
            const box = wrapper.getBoundingClientRect();
            const width = Math.min(DISPLAY_WIDTH, window.innerWidth - 32);
            const preferred = Math.max(box.width - width, 16 - box.left);
            this.displayLeft = Math.min(preferred, window.innerWidth - 16 - width - box.left);
            this.open = "display";
        },
        ...window.filterDropdown,
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
            SPEND_PARAMS.forEach((name) => (this[name] = ""));
            this._apply();
        },

        openFilter(key = null, anchorEl = null) {
            this.anchor = this._anchorTo(anchorEl || this.$refs.filterButton);
            this.propertyQuery = "";
            this.open = "filter";
            if (key) this.drill(key);
            else {
                this.property = null;
                this.$nextTick(() => this._focusMenu());
            }
        },
        closeFilter(refocus = true) {
            if (this.open !== "filter") return;
            this.open = null;
            this.property = null;
            if (refocus) this.$refs.filterButton.focus();
        },
        drill(key) {
            this.property = key;
            this.valueQuery = "";
            this.findChoices();
            this.$nextTick(() => this._focusMenu());
        },
        findChoices() {
            const box = this.$root.querySelector(`[data-filter-choices="${this.property}"]`);
            if (box) this._loadChoices(box);
        },
        _loadChoices(box) {
            const params = this._params();
            const find = this.valueQuery.trim();
            if (find) params.set("find", find);
            const names = [...(box.dataset.reloadOn ?? "").split(" "), "find"];
            const loadKey = names.map((name) => params.get(name)).join("|");
            if (box.dataset.loadedFor === loadKey) return;
            box.dataset.loadedFor = loadKey;
            htmx.ajax("GET", box.dataset.choicesUrl + "?" + params.toString(), {
                source: box,
                target: box,
                swap: "innerHTML",
            }).catch(() => {});
        },
        back() {
            this.property = null;
            this.$nextTick(() => this._focusMenu());
        },
        choicesFailed(box) {
            delete box.dataset.loadedFor;
            const message = document.createElement("p");
            message.className = "filter-menu__label filter-menu__loading";
            message.textContent = box.dataset.errorMessage;
            box.replaceChildren(message);
        },
        matches(text, query) {
            return (text || "").toLowerCase().includes(query.trim().toLowerCase());
        },
        isSet(key) {
            return key === "time" ? this.timeActive : !!this[key];
        },
        valueLabel(key) {
            return key === "time" ? this.timeLabel : this.labelFor(key);
        },
        onKey(event) {
            if (event.key.toLowerCase() !== "f" || event.repeat || event.defaultPrevented) return;
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            if (event.target?.isContentEditable || event.target?.closest?.(TYPING) || this.open !== null) return;
            event.preventDefault();
            this.openFilter();
        },
        _anchorTo(el) {
            const root = this.$root.getBoundingClientRect();
            const box = el.getBoundingClientRect();
            const width = Math.min(MENU_WIDTH, window.innerWidth - 32);
            const left = Math.max(0, Math.min(box.left - root.left, root.width - width));
            return { left, top: box.bottom - root.top + 6 };
        },
        _focusMenu() {
            const view = this.property
                ? this.$refs.filterMenu.querySelector(`[data-filter-panel="${this.property}"]`)
                : this.$refs.filterMenu.firstElementChild;
            (view?.querySelector("[data-filter-focus]") || view?.querySelector("a, button:not(.filter-menu__back)"))?.focus();
        },

        get activeCount() {
            const time = this.range || this.from || this.to ? 1 : 0;
            return CHIP_KEYS.filter((key) => this[key]).length + time + (this.archived ? 1 : 0);
        },
        get timeLabel() {
            if (this.range) return this.labelFor("range");
            if (this.from || this.to) return (this.from || "…") + " – " + (this.to || "…");
            return "";
        },
        get timeActive() {
            return !!(this.range || this.from || this.to);
        },
        get hasSpendBanner() {
            return SPEND_BANNER_PARAMS.some((name) => this[name]);
        },
    }));
});
