/**
 * Alpine component: env pill + popover.
 *
 * Constructor args (passed via x-data):
 *   envs:          Array<{id, name, scope, is_default, summary}> — server-rendered env list.
 *   selectedId:    string — UUID of the currently selected env; '' means GLOBAL default.
 *   onChangeEvent: optional window event name dispatched on selection change with {detail: {id}}.
 *   createUrl:     URL the drawer fetches when "+ New environment" is clicked.
 *
 * Window events:
 *   env-created (received): prepend the new env to the local list and select it.
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
                    this.highlightIndex = Math.max(0, this.filteredEnvs.findIndex(e => this._isSelected(e)));
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
            const q = this.query.trim().toLowerCase();
            const matches = q ? this.envs.filter(e => e.name.toLowerCase().includes(q)) : [...this.envs];
            matches.sort((a, b) => {
                if (a.is_default && !b.is_default) return -1;
                if (!a.is_default && b.is_default) return 1;
                if (a.scope !== b.scope) return a.scope.localeCompare(b.scope);
                return a.name.localeCompare(b.name);
            });
            return matches;
        },

        moveHighlight(delta) {
            const n = this.filteredEnvs.length;
            if (n === 0) return;
            this.highlightIndex = (this.highlightIndex + delta + n) % n;
        },

        selectHighlighted() {
            const row = this.filteredEnvs[this.highlightIndex];
            if (row) this.select(this._isDefaultRow(row) ? "" : row.id);
        },

        _isDefaultRow(env) {
            return env.scope === "global" && env.is_default;
        },

        _isSelected(env) {
            if (this._isDefaultRow(env) && !this.selectedId) return true;
            return env.id === this.selectedId;
        },

        get pillLabel() {
            const env = this.selectedId ? this.envs.find(e => e.id === this.selectedId) : null;
            if (env && !this._isDefaultRow(env)) return {name: env.name, scopeTag: env.scope};
            return {name: this._defaultLabel(), scopeTag: ""};
        },

        _defaultLabel() {
            return this.$root.dataset.defaultLabel || "Default";
        },
    }));
});
