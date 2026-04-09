/**
 * Shared Alpine.js component for async repository search via x-combobox.
 *
 * Usage:
 *   <div x-data="repoSearch('initial/value')">
 *     <div x-combobox x-model="selected" @change="..." nullable>
 *       <input type="text" x-combobox:input
 *              :display-value="v => v || ''"
 *              @input="search($event.target.value)" ...>
 *       ...options template using `results` array (each item has .slug, .name)
 *     </div>
 *   </div>
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("repoSearch", (initial = "") => ({
        selected: initial || null,
        results: [],
        isLoading: false,
        _timer: null,
        _controller: null,

        search(query) {
            clearTimeout(this._timer);
            if (query.length < 2) {
                this.results = [];
                return;
            }
            this._timer = setTimeout(() => this._fetch(query), 300);
        },

        async _fetch(query) {
            this._controller?.abort();
            this._controller = new AbortController();
            this.isLoading = true;
            try {
                const resp = await fetch(
                    "/api/codebase/repositories/search?q=" + encodeURIComponent(query),
                    { signal: this._controller.signal }
                );
                this.results = resp.ok ? await resp.json() : [];
            } catch (e) {
                if (e.name !== "AbortError") this.results = [];
            } finally {
                this.isLoading = false;
            }
        },
    }));
});
