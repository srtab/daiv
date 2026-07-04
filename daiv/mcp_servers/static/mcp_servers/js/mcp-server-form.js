/**
 * Alpine component: "Test connection" button on the MCP server form.
 *
 * POSTs the live form (transport, url, headers formset, csrf token) to the
 * mcp_servers:test endpoint. On success it swaps the tool-filter field for a
 * checkbox list built from the discovered tools, using the SAME markup the
 * template renders server-side (data-tool-list / data-tool-row / .mcp-tool-*),
 * so the client-side filter below works on both. The checkboxes reuse the field
 * name (`tool_filter_items`) — the form field accepts both POST shapes (see
 * MultiValueTextarea) — and the original inputs are disabled so they are not
 * submitted alongside. Also holds the segmented-control state (transport,
 * filterMode) for the pill toggles and the tool-filter progressive disclosure.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("mcpTestConnection", ({ testUrl, transport, filterMode, filterPlaceholder, notInListLabel, readonlyLabel, writableLabel }) => ({
        state: "idle", // idle | testing | ok | error
        error: "",
        toolCount: 0,
        transport,
        filterMode,
        filterPlaceholder,
        notInListLabel,
        readonlyLabel,
        writableLabel,
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
            // Read the current selection while any previously generated list is
            // still present and enabled — it holds the user's live selection.
            const selected = new Set(this.currentSelection(container));
            const names = tools.map((t) => t.name).filter(Boolean);
            const extras = [...selected].filter((n) => !names.includes(n));
            // Replace, don't accumulate: drop a list a previous test generated.
            container.querySelectorAll("[data-generated]").forEach((el) => el.remove());
            // Hide + disable the current widget (textarea or server-rendered rows):
            // disabled inputs are not submitted, so the generated checkboxes own the name.
            for (const el of container.children) el.classList.add("hidden");
            container.querySelectorAll("textarea, input").forEach((el) => (el.disabled = true));

            const list = document.createElement("div");
            list.dataset.generated = "true";
            list.setAttribute("data-tool-list", "");

            const filter = document.createElement("input");
            filter.type = "text";
            filter.setAttribute("data-tool-filter", "");
            filter.placeholder = this.filterPlaceholder;
            filter.className = "mb-2";
            list.append(filter);

            const scroll = document.createElement("div");
            scroll.className = "mcp-tool-list__scroll";
            for (const name of [...names, ...extras]) {
                const tool = tools.find((t) => t.name === name);
                const available = Boolean(tool);
                const row = document.createElement("label");
                row.setAttribute("data-tool-row", "");
                row.dataset.toolName = name;
                row.className = "mcp-tool-row";

                const box = document.createElement("input");
                box.type = "checkbox";
                box.name = "tool_filter_items";
                box.value = name;
                box.checked = selected.has(name);
                box.className = "mt-0.5";

                const wrap = document.createElement("span");
                wrap.className = "min-w-0";
                const nameRow = document.createElement("span");
                nameRow.className = "flex min-w-0 items-center gap-2";
                const nameEl = document.createElement("span");
                nameEl.className = "mcp-tool-row__name min-w-0 truncate";
                nameEl.textContent = name;
                nameRow.append(nameEl);
                // Advisory read/write pill, mirroring the server-rendered template:
                // strict true/false only — null/undefined (unannotated) → no pill.
                const readOnly = available ? tool.read_only : null;
                if (readOnly === true || readOnly === false) {
                    const pill = document.createElement("span");
                    pill.className = `mcp-tool-row__pill mcp-tool-row__pill--${readOnly ? "ro" : "rw"}`;
                    pill.textContent = readOnly ? this.readonlyLabel : this.writableLabel;
                    nameRow.append(pill);
                }
                wrap.append(nameRow);

                const descText = available ? tool.description || "" : this.notInListLabel;
                if (descText) {
                    const descEl = document.createElement("span");
                    descEl.className = "mcp-tool-row__desc";
                    descEl.textContent = descText;
                    if (available && tool.description) descEl.title = tool.description;
                    wrap.append(descEl);
                }
                row.append(box, wrap);
                scroll.append(row);
            }
            list.append(scroll);
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

// Delegated client-side filter: narrows the rows of whichever [data-tool-list]
// the typed-in [data-tool-filter] belongs to. Works for both the server-rendered
// list and the one swapFilterField generates.
document.addEventListener("input", (e) => {
    const input = e.target.closest("[data-tool-filter]");
    if (!input) return;
    const list = input.closest("[data-tool-list]");
    if (!list) return;
    const q = input.value.trim().toLowerCase();
    list.querySelectorAll("[data-tool-row]").forEach((row) => {
        const name = (row.dataset.toolName || "").toLowerCase();
        row.classList.toggle("hidden", Boolean(q) && !name.includes(q));
    });
});
