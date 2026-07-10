/**
 * Sessions filter bar — client-driven, results-only swap.
 *
 * The filter bar is never inside the swapped region, so it never jumps. It holds
 * filter state initialized FROM THE URL (so first paint and back/forward both stay
 * correct), renders its own active highlights reactively, and swaps only #session-results.
 *
 * History is manual: swapResults() pushState()s the new URL once the swap succeeds
 * (the popstate re-swap passes { push: false } so replaying history doesn't re-push).
 * A single popstate handler re-swaps and tells the bar to re-read the URL. No
 * HTMX-managed history is used.
 */

// Monotonic swap token: only the newest swap may commit the URL and clear the loading
// state, so overlapping requests (fast clicks, a debounced apply landing near a click)
// can't have a stale one win.
let swapSeq = 0;

// One swap path for everything (filters + pagination + popstate). The URL is committed
// to history ONLY after the results actually swap in, so the address bar can never
// describe results the user isn't seeing; a failed swap is surfaced as a toast.
function swapResults(url, { push = true } = {}) {
    const box = document.getElementById("session-results");
    if (!box) return;
    const seq = ++swapSeq;
    box.classList.add("session-results--loading");

    // A swap only happens on a 2xx; a 4xx/5xx or network error leaves the old content in
    // place. htmx.ajax's promise resolves in all of those cases, so detect a REAL swap via
    // the swap event — the same signal session_list.html keys the SSE re-arm on.
    let swapped = false;
    const onSwap = () => {
        swapped = true;
    };
    box.addEventListener("htmx:afterSwap", onSwap, { once: true });

    htmx
        .ajax("GET", url, { target: "#session-results", swap: "innerHTML" })
        .catch(() => {}) // network/send error: no swap fired, handled below
        .finally(() => {
            box.removeEventListener("htmx:afterSwap", onSwap);
            if (seq !== swapSeq) return; // a newer swap superseded this one — let it win
            // innerHTML swaps replace only the contents, so `box` is still the live node.
            box.classList.remove("session-results--loading");
            if (swapped) {
                if (push) window.history.pushState({}, "", url);
            } else {
                window.showToast("Couldn't update the session list — check your connection and try again.", "error");
            }
        });
}

// Pagination links inside the results fragment are re-marked on every swap; a delegated
// listener keeps working across swaps without re-binding.
document.addEventListener("click", (event) => {
    const link = event.target.closest("#session-results a[data-page-swap]");
    if (!link) return;
    // Preserve open-in-new-tab/window: only hijack an unmodified primary-button click.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
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
        range: "",
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
        clearParam(key) {
            this[key] = "";
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
                this.schedule ||
                this.batch
            );
        },
    }));
});
