/**
 * Alpine component: multi-select user chip picker backed by /api/accounts/users/search.
 *
 * Usage (inside a form):
 *   <div x-data="subscriberPicker({ initial: [...users] })">
 *     <select name="subscribers" id="id_subscribers" multiple class="hidden">
 *       <template x-for="u in selected" :key="u.id">
 *         <option :value="u.id" selected x-text="u.username"></option>
 *       </template>
 *     </select>
 *     ...chips, search input, results dropdown...
 *   </div>
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("subscriberPicker", ({ initial = [] } = {}) => ({
        selected: [...initial],
        query: "",
        results: [],
        isLoading: false,
        _timer: null,
        _controller: null,

        isSelected(id) {
            return this.selected.some((u) => u.id === id);
        },

        add(user) {
            if (!this.isSelected(user.id)) {
                this.selected.push(user);
            }
            this.query = "";
            this.results = [];
        },

        remove(id) {
            this.selected = this.selected.filter((u) => u.id !== id);
        },

        search(value) {
            this.query = value;
            clearTimeout(this._timer);
            if (value.length < 2) {
                this.results = [];
                return;
            }
            this._timer = setTimeout(() => this._fetch(value), 300);
        },

        async _fetch(value) {
            this._controller?.abort();
            this._controller = new AbortController();
            this.isLoading = true;
            try {
                const excludeIds = this.selected.map((u) => u.id).join(",");
                const params = new URLSearchParams({ q: value });
                if (excludeIds) params.set("exclude", excludeIds);
                const resp = await fetch(
                    "/api/accounts/users/search?" + params.toString(),
                    { signal: this._controller.signal },
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
