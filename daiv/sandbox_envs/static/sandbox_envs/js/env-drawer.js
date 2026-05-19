/**
 * Alpine component: side drawer for creating, editing, OR deleting a sandbox environment.
 *
 * Event contract (all on window):
 *   open-env-drawer  {detail: {mode: 'create'|'edit'|'delete', url: string}} → opens; lazy-loads the body fragment via HTMX
 *   close-env-drawer                                                          → closes
 *   env-created / env-updated / env-deleted                                   → host page refreshes; this file also closes
 *
 * The body fragment is fetched on every open against the provided URL via
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
        deleteTitle: labels.deleteTitle || "Delete sandbox environment",
        createSubtitle: labels.createSubtitle || "Create an environment without leaving this page.",
        editSubtitle: labels.editSubtitle || "Edit without leaving this page.",
        deleteSubtitle: labels.deleteSubtitle || "Confirm deletion of this environment.",

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
            return ({edit: this.editTitle, delete: this.deleteTitle})[this.mode] ?? this.createTitle;
        },

        get subtitle() {
            return ({edit: this.editSubtitle, delete: this.deleteSubtitle})[this.mode] ?? this.createSubtitle;
        },
    }));
});

["env-created", "env-updated", "env-deleted"].forEach((name) => {
    window.addEventListener(name, () => window.dispatchEvent(new CustomEvent("close-env-drawer")));
});
