/**
 * Alpine component: the configuration page's section switcher below `lg`.
 *
 * The surface is a `.picker-popover`, so it is a bottom sheet at every width this
 * component exists at — the sidebar takes over before 1100px, where the popover form
 * would start.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("configSectionPicker", () => ({
        open: false,
        _announceOpen: null,

        init() {
            this._announceOpen = surfaceGroup.join(() => this.close());
        },

        toggle() {
            this.open = !this.open;
            if (this.open) {
                this._announceOpen();
                this.$nextTick(() => this.$refs.current?.focus());
            }
        },

        close() {
            this.open = false;
        },
    }));
});
