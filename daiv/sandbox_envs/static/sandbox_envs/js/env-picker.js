/**
 * Alpine component: env pill + popover.
 *
 * Constructor args (passed via x-data):
 *   envs:       Array<{id, name, scope, base_image, cpus, memory_bytes, network_enabled, is_default, summary}>
 *   selectedId: string ('' for global default)
 *   onChangeEvent: optional window event name dispatched on selection change with {detail: {id}}
 *
 * Window events:
 *   env-created (received): prepend the new env to the local list and select it
 *   open-env-drawer (sent): opens the create drawer; URL is read from a data attribute
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

        init() {
            window.addEventListener("env-created", (e) => this.onEnvCreated(e.detail));
        },

        toggle() {
            this.open = !this.open;
            if (this.open) {
                this.query = "";
                this.$nextTick(() => {
                    this.highlightIndex = Math.max(0, this.filtered().findIndex(e => this._isSelected(e)));
                    this.$refs.search?.focus();
                });
            }
        },

        close() {
            this.open = false;
        },

        select(id) {
            this.selectedId = id || "";
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

        filtered() {
            const q = this.query.trim().toLowerCase();
            const matches = q ? this.envs.filter(e => e.name.toLowerCase().includes(q)) : [...this.envs];
            // Pin the GLOBAL default to the top.
            matches.sort((a, b) => {
                if (a.is_default && !b.is_default) return -1;
                if (!a.is_default && b.is_default) return 1;
                if (a.scope !== b.scope) return a.scope.localeCompare(b.scope);
                return a.name.localeCompare(b.name);
            });
            return matches;
        },

        moveHighlight(delta) {
            const n = this.filtered().length;
            if (n === 0) return;
            this.highlightIndex = (this.highlightIndex + delta + n) % n;
        },

        selectHighlighted() {
            const row = this.filtered()[this.highlightIndex];
            if (row) this.select(this._isDefaultRow(row) ? "" : row.id);
        },

        _isDefaultRow(env) {
            return env.scope === "global" && env.is_default;
        },

        _isSelected(env) {
            if (this._isDefaultRow(env) && !this.selectedId) return true;
            return env.id === this.selectedId;
        },

        // Computed: what the pill should render.
        get pillLabel() {
            if (!this.selectedId) return {name: this._defaultLabel(), scopeTag: ""};
            const env = this.envs.find(e => e.id === this.selectedId);
            if (!env) return {name: this._defaultLabel(), scopeTag: ""};
            if (this._isDefaultRow(env)) return {name: this._defaultLabel(), scopeTag: ""};
            return {name: env.name, scopeTag: env.scope};
        },

        _defaultLabel() {
            return this.$root.dataset.defaultLabel || "Default";
        },
    }));
});
