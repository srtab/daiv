/**
 * Alpine component: the configuration page's section switcher, below `--breakpoint-popover`.
 *
 * The trigger and `.picker-popover`'s sheet form switch on that one number, so the surface
 * is a bottom sheet at every width this component exists at.
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
