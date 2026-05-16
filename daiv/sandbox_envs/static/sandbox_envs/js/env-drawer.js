/**
 * Alpine component: side drawer for creating OR editing a sandbox environment.
 *
 * Event contract (all on window):
 *   open-env-drawer  {detail: {mode: 'create'|'edit', url: string}} → opens; lazy-loads the form fragment via HTMX
 *   close-env-drawer                                                 → closes
 *   env-created / env-updated                                        → host page refreshes; this file also closes
 *
 * The form fragment is fetched on every open against the provided URL via
 * htmx.ajax(); errors are surfaced as an inline banner with a Retry button.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("envDrawer", (labels = {}) => ({
        open: false,
        mode: "create",
        url: "",
        error: false,
        createTitle: labels.createTitle || "New sandbox environment",
        editTitle: labels.editTitle || "Edit sandbox environment",
        createSubtitle: labels.createSubtitle || "Create an environment without leaving this page.",
        editSubtitle: labels.editSubtitle || "Edit without leaving this page.",

        init() {
            this.$el.addEventListener("htmx:afterRequest", (e) => {
                this.error = !e.detail.successful;
            });
            this.$el.addEventListener("htmx:sendError", () => {
                this.error = true;
            });
        },

        openDrawer({mode = "create", url} = {}) {
            this.mode = mode;
            this.url = url || "";
            this.open = true;
            this.error = false;
            this.$nextTick(() => this.loadBody());
        },

        loadBody() {
            if (!this.url) return;
            htmx.ajax("GET", this.url, {target: this.$refs.body, swap: "innerHTML"});
        },

        retryLoad() {
            this.error = false;
            this.loadBody();
        },

        close() {
            this.open = false;
        },

        get title() {
            return this.mode === "edit" ? this.editTitle : this.createTitle;
        },

        get subtitle() {
            return this.mode === "edit" ? this.editSubtitle : this.createSubtitle;
        },
    }));
});

window.addEventListener("env-created", () => {
    window.dispatchEvent(new CustomEvent("close-env-drawer"));
});
window.addEventListener("env-updated", () => {
    window.dispatchEvent(new CustomEvent("close-env-drawer"));
});
