/**
 * Alpine component: MCP server selector for the chat hero / composer.
 *
 * Fetches the pool lazily from ``poolUrl`` (api:mcp_selection_pool) and pre-loads
 * on init so the pill label is seeded before the user opens the popover. Maintains a
 * ``selected`` Set (server names) seeded from the pool's ``is_default`` flags.
 * Selection changes fire ``daiv:mcp-changed`` on window so the chat root can forward
 * checked names in ``forwardedProps.mcp_servers`` on the first turn only.
 *
 * Constructor args:
 *   poolUrl: string — ``api:mcp_selection_pool`` endpoint URL.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("mcpChatPicker", ({ poolUrl = "" } = {}) => ({
        poolUrl,
        pool: [],
        selected: new Set(),
        open: false,
        status: "idle",

        get globalPool() {
            return this.pool.filter((e) => e.scope === "global");
        },
        get userPool() {
            return this.pool.filter((e) => e.scope === "user");
        },
        get pillLabel() {
            return this.selected.size === 0 ? "MCP" : `MCP · ${this.selected.size}`;
        },
        // Non-zero badge when the selection differs from the all-defaults state.
        get checkedDelta() {
            const defaultCount = this.pool.filter((e) => e.is_default).length;
            return this.selected.size !== defaultCount ? this.selected.size : 0;
        },

        openPopover() {
            this.open = !this.open;
            if (this.open) this._load();
        },

        close() {
            this.open = false;
        },

        toggleServer(entry) {
            const next = new Set(this.selected);
            if (next.has(entry.name)) {
                next.delete(entry.name);
            } else {
                next.add(entry.name);
            }
            this.selected = next;
            this._dispatch();
        },

        async _load() {
            if (this.status === "loading" || this.status === "ready") return;
            this.status = "loading";
            try {
                const resp = await fetch(this.poolUrl, { credentials: "include" });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                this.pool = await resp.json();
                this.selected = new Set(
                    this.pool.filter((e) => e.is_default).map((e) => e.name)
                );
                this.status = "ready";
                this._dispatch();
            } catch (err) {
                console.error("mcp-chat-picker: load failed", err);
                this.status = "error";
            }
        },

        _dispatch() {
            try {
                window.dispatchEvent(new CustomEvent("daiv:mcp-changed", {
                    detail: { servers: [...this.selected] },
                }));
            } catch (err) {
                console.error("mcp-chat-picker: dispatch error", err);
            }
        },

        init() {
            this._load();
        },
    }));
});
