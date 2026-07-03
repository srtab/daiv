/**
 * Alpine component: "Test connection" button on the MCP server form.
 *
 * POSTs the live form (transport, url, headers formset, csrf token) to the
 * mcp_servers:test endpoint. On success it swaps the tool-filter free-text
 * field for a checkbox list built from the discovered tools. The checkboxes
 * reuse the field name (`tool_filter_items`) — the form field accepts both
 * POST shapes (see MultiValueTextarea) — and the original inputs are disabled
 * so they are not submitted alongside.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("mcpTestConnection", ({ testUrl }) => ({
        state: "idle", // idle | testing | ok | error
        error: "",
        toolCount: 0,
        async test() {
            this.state = "testing";
            this.error = "";
            const form = this.$root.closest("form") || this.$root;
            try {
                const resp = await fetch(testUrl, { method: "POST", body: new FormData(form) });
                const json = await resp.json();
                if (json.ok) {
                    this.state = "ok";
                    this.toolCount = json.tools.length;
                    this.swapFilterField(json.tools);
                } else {
                    this.state = "error";
                    this.error = json.error || "Connection failed";
                }
            } catch (err) {
                this.state = "error";
                this.error = String(err);
            }
        },
        swapFilterField(tools) {
            const container = this.$refs.filterItems;
            if (!container) return;
            const selected = new Set(this.currentSelection(container));
            const names = tools.map((t) => t.name).filter(Boolean);
            const extras = [...selected].filter((n) => !names.includes(n));
            // Disable + hide the current widget (textarea or server-rendered checkboxes):
            // disabled inputs are not submitted, so the generated checkboxes own the name.
            for (const el of container.children) el.classList.add("hidden");
            container.querySelectorAll("textarea, input").forEach((el) => (el.disabled = true));
            const list = document.createElement("div");
            list.className = "space-y-1";
            for (const name of [...names, ...extras]) {
                const label = document.createElement("label");
                label.className = "flex items-center gap-2";
                const box = document.createElement("input");
                box.type = "checkbox";
                box.name = "tool_filter_items";
                box.value = name;
                box.checked = selected.has(name);
                const tool = tools.find((t) => t.name === name);
                const text = document.createElement("span");
                text.textContent = tool && tool.description ? `${name} — ${tool.description}` : name;
                label.append(box, text);
                list.append(label);
            }
            container.append(list);
        },
        currentSelection(container) {
            // Enabled checkboxes first: after a previous swap the textarea is disabled/stale,
            // and the user's current selection lives in the generated boxes.
            const activeBoxes = [...container.querySelectorAll("input[type=checkbox]:not(:disabled)")];
            if (activeBoxes.length) {
                return activeBoxes.filter((el) => el.checked).map((el) => el.value);
            }
            const textarea = container.querySelector("textarea");
            if (textarea && !textarea.disabled) {
                return textarea.value.split("\n").map((s) => s.trim()).filter(Boolean);
            }
            return [...container.querySelectorAll("input[type=checkbox]:checked")].map((el) => el.value);
        },
    }));
});
