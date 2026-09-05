/**
 * Thin Alpine state shell for the schedule subscriber picker.
 *
 * Mirrors the promptBox/repo-picker pattern: owns the chip list and the open/close
 * state of an HTMX-driven popover. The result list is server-rendered into
 * `#user-picker-list` and attaches back into this component via
 * `@click="addUser(...)"` — Alpine's MutationObserver picks the directive up when
 * HTMX swaps it in.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("subscriberPicker", ({ initial = [] } = {}) => ({
        selected: [...initial],
        popover: false,
        loading: false,
        _announceOpen: null,

        init() {
            this._announceOpen = surfaceGroup.join(() => this.closePicker());
            this.$el.addEventListener("htmx:beforeRequest", (e) => {
                if (e.target === this.$refs.userSearch) this.loading = true;
            });
            this.$el.addEventListener("htmx:afterSwap", (e) => {
                if (e.target === this.$refs.userPickerList) this.loading = false;
            });
            for (const event of ["htmx:sendError", "htmx:responseError"]) {
                this.$el.addEventListener(event, (e) => {
                    if (e.target !== this.$refs.userSearch) return;
                    this.loading = false;
                    pickerError.showIn(this.$refs.userPickerList);
                });
            }
        },

        get excludeCsv() {
            return this.selected.map((u) => u.id).join(",");
        },

        isSelected(id) {
            return this.selected.some((u) => u.id === id);
        },

        openPicker() {
            this.popover = true;
            this._announceOpen();
            this.$nextTick(() => {
                // Reset to a clean empty state — no server round-trip until the user
                // types ≥ PICKER_USERS_MIN_QUERY chars and `input changed` fires.
                const input = this.$refs.userSearch;
                const list = this.$refs.userPickerList;
                if (input) {
                    input.value = "";
                    input.focus();
                }
                if (list) list.innerHTML = "";
                this.loading = false;
            });
        },

        closePicker() {
            this.popover = false;
        },

        addUser(user) {
            if (!this.isSelected(user.id)) {
                this.selected.push(user);
            }
            this.closePicker();
        },

        remove(id) {
            this.selected = this.selected.filter((u) => u.id !== id);
        },
    }));
});
