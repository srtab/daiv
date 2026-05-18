/**
 * Alpine component: env pill + popover.
 *
 * Constructor args (passed via x-data):
 *   envs:            Array<{id, name, scope, is_default, summary}> — server-rendered env list.
 *   selectedId:      string — UUID of the currently selected env; '' means Auto (resolved at runtime).
 *   onChangeEvent:   optional window event name dispatched on selection change with {detail: {id}}.
 *   createUrl:       URL the drawer fetches when "+ New environment" is clicked.
 *
 * Window events:
 *   env-created (received): if not already present, prepend the new env; then select it.
 *   open-env-drawer (sent): opens the create drawer with {mode, url} payload.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("envPicker", ({envs = [], selectedId = "", onChangeEvent = "", createUrl = ""} = {}) => ({
        envs: [...envs],
        selectedId: selectedId || "",
        onChangeEvent,
        createUrl,
        open: false,
        query: "",
        highlightIndex: 0,
        _envCreatedHandler: null,

        init() {
            this._envCreatedHandler = (e) => this.onEnvCreated(e.detail);
            window.addEventListener("env-created", this._envCreatedHandler);
            if (this.selectedId && !this.envs.some(e => e.id === this.selectedId)) {
                this.select("");
            }
        },

        destroy() {
            if (this._envCreatedHandler) {
                window.removeEventListener("env-created", this._envCreatedHandler);
            }
        },

        toggle() {
            this.open = !this.open;
            if (this.open) {
                this.query = "";
                this.$nextTick(() => {
                    this.highlightIndex = this._isAutoSelected()
                        ? -1
                        : Math.max(0, this.filteredEnvs.findIndex(e => this._isSelected(e)));
                    this.$refs.search?.focus();
                });
            }
        },

        close() {
            this.open = false;
        },

        select(id) {
            const normalised = id || "";
            if (normalised === this.selectedId) {
                this.close();
                return;
            }
            this.selectedId = normalised;
            this.close();
            if (this.onChangeEvent) {
                window.dispatchEvent(new CustomEvent(this.onChangeEvent, {detail: {id: this.selectedId}}));
            }
        },

        openCreate() {
            window.dispatchEvent(new CustomEvent("open-env-drawer", {
                detail: {mode: "create", url: this.createUrl},
            }));
            this.close();
        },

        onEnvCreated(env) {
            if (!env?.id) return;
            if (!this.envs.some(e => e.id === env.id)) {
                this.envs = [env, ...this.envs];
            }
            this.select(env.id);
        },

        get filteredEnvs() {
            // The GLOBAL default is reachable via the Auto row — listing it again would be redundant.
            // Scope-guard the filter so a future USER-scoped default (if introduced) stays visible.
            const q = this.query.trim().toLowerCase();
            const visible = this.envs.filter(e => !(e.scope === "global" && e.is_default));
            const matches = q ? visible.filter(e => e.name.toLowerCase().includes(q)) : visible;
            matches.sort((a, b) => {
                if (a.scope !== b.scope) return a.scope.localeCompare(b.scope);
                return a.name.localeCompare(b.name);
            });
            return matches;
        },

        moveHighlight(delta) {
            // highlightIndex === -1 means the Auto row is highlighted.
            // The range is [-1, filteredEnvs.length - 1].
            const n = this.filteredEnvs.length;
            const min = -1;
            const max = n - 1;
            const next = this.highlightIndex + delta;
            if (next < min) {
                this.highlightIndex = max;
            } else if (next > max) {
                this.highlightIndex = min;
            } else {
                this.highlightIndex = next;
            }
        },

        selectHighlighted() {
            if (this.highlightIndex === -1) {
                this.select("");
                return;
            }
            const row = this.filteredEnvs[this.highlightIndex];
            if (row) this.select(row.id);
        },

        _isAutoSelected() {
            return !this.selectedId;
        },

        _isSelected(env) {
            return env.id === this.selectedId;
        },

        get pillLabel() {
            if (!this.selectedId) return {name: "Auto", scopeTag: ""};
            const env = this.envs.find(e => e.id === this.selectedId);
            if (env) return {name: env.name, scopeTag: env.scope};
            return {name: "Auto", scopeTag: ""};
        },
    }));
});
