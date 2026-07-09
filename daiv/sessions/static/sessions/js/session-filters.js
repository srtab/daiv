/**
 * Alpine.js component for the sessions filter bar.
 *
 * Status pills and Type/Time preset options are plain <a> links (deep-linkable, no-JS
 * safe). This component only handles: dropdown open/close state, the debounced search
 * box, and the custom date-range submit — each rewrites the URL querystring and reloads.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("filterBar", (initialQuery) => ({
        open: null, // id of the open dropdown, or null
        q: initialQuery || "",
        toggle(id) {
            this.open = this.open === id ? null : id;
        },
        _go(mutate) {
            const params = new URLSearchParams(window.location.search);
            mutate(params);
            params.delete("page");
            window.location.search = params.toString();
        },
        setParam(key, value) {
            this._go((p) => (value ? p.set(key, value) : p.delete(key)));
        },
        setCustomRange(from, to) {
            this._go((p) => {
                p.delete("range");
                if (from) p.set("date_from", from);
                else p.delete("date_from");
                if (to) p.set("date_to", to);
                else p.delete("date_to");
            });
        },
    }));
});
