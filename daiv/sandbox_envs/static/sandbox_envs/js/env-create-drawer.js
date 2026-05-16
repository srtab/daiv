/**
 * Alpine component: side drawer for creating a sandbox environment.
 *
 * Event contract:
 *   open-env-create-drawer  (window) → opens; lazy-loads the form fragment via HTMX
 *   close-env-create-drawer (window) → closes
 *   env-created             (window) → host page updates the picker; this file
 *                                       also listens and triggers close
 *
 * The form fragment is fetched on first open by triggering `load-form` against
 * `$refs.body` (an htmx-bound element). HTMX errors during load or submit are
 * captured via `htmx:responseError` / `htmx:sendError` and surfaced as an
 * inline banner; reopening retries the load if the first attempt failed.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("envCreateDrawer", () => ({
        open: false,
        loaded: false,
        error: false,

        init() {
            this.$el.addEventListener("htmx:afterRequest", (e) => {
                if (e.detail.successful) {
                    this.error = false;
                    if (e.target === this.$refs.body) this.loaded = true;
                } else {
                    this.error = true;
                }
            });
            this.$el.addEventListener("htmx:sendError", () => {
                this.error = true;
            });
        },

        openDrawer() {
            this.open = true;
            if (!this.loaded) this.retryLoad();
        },

        retryLoad() {
            this.error = false;
            this.$nextTick(() => htmx.trigger(this.$refs.body, "load-form"));
        },

        close() {
            this.open = false;
        },
    }));
});

window.addEventListener("env-created", () => {
    window.dispatchEvent(new CustomEvent("close-env-create-drawer"));
});
