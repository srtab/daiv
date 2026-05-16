/**
 * Alpine component: editable env-vars list for the sandbox environment form.
 *
 * Adds live name validation (regex from sandbox_envs.models._ENV_VAR_NAME_RE)
 * and exposes a paste-overlay setter for Task 9's overlay component.
 */
const ENV_VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function parseDotenv(text) {
    const entries = [];
    const invalid = [];
    const lines = (text || "").split(/\r?\n/);
    lines.forEach((raw, idx) => {
        const line = raw.trim();
        if (!line || line.startsWith("#")) return;
        const eqIdx = line.indexOf("=");
        if (eqIdx <= 0) { invalid.push(idx + 1); return; }
        const name = line.slice(0, eqIdx).trim();
        let value = line.slice(eqIdx + 1).trim();
        if (!ENV_VAR_NAME_RE.test(name)) { invalid.push(idx + 1); return; }
        const single = value.startsWith("'") && value.endsWith("'") && value.length >= 2;
        const double = value.startsWith('"') && value.endsWith('"') && value.length >= 2;
        if (single) {
            value = value.slice(1, -1);
        } else if (double) {
            value = value.slice(1, -1)
                .replace(/\\n/g, "\n")
                .replace(/\\t/g, "\t")
                .replace(/\\\\/g, "\\");
        }
        entries.push({name, value});
    });
    return {entries, invalidLines: invalid};
}

function envVarsEditorState(initial = []) {
    return {
        rows: Array.isArray(initial) ? initial.map((r) => ({...r})) : [],

        rowError(i) {
            const row = this.rows[i];
            if (!row) return "";
            const name = (row.name || "").trim();
            if (!name && !row.value) return "";
            if (!ENV_VAR_NAME_RE.test(name)) return "Invalid name";
            for (let j = 0; j < i; j++) {
                if ((this.rows[j].name || "").trim() === name && (this.rows[j].value || this.rows[j].name)) {
                    return "Duplicate name";
                }
            }
            return "";
        },

        get serialised() {
            return this.rows
                .filter((r) => (r.name || "").trim() || (r.value || ""))
                .map((r) => ({name: (r.name || "").trim(), value: r.value || "", is_secret: !!r.is_secret}));
        },

        addRow() {
            this.rows.push({name: "", value: "", is_secret: false, has_existing_value: false});
        },

        removeRow(i) {
            this.rows.splice(i, 1);
        },

        mergeImport(entries, replace) {
            const incoming = entries.map((e) => ({name: e.name, value: e.value, is_secret: false, has_existing_value: false}));
            if (replace) {
                this.rows = incoming;
                return;
            }
            const existing = new Set(this.rows.map((r) => r.name));
            for (const row of incoming) {
                if (!existing.has(row.name)) this.rows.push(row);
            }
        },
    };
}

document.addEventListener("alpine:init", () => {
    Alpine.data("envVarsEditor", envVarsEditorState);

    Alpine.data("envPasteOverlay", () => ({
        open: false,
        text: "",
        mode: "merge",
        parsed: {entries: [], invalidLines: []},

        onOpen() {
            this.open = true;
            this.text = "";
            this.parsed = {entries: [], invalidLines: []};
        },

        onInput() { this.parsed = parseDotenv(this.text); },

        close() { this.open = false; },

        importNow() {
            if (!this.parsed.entries.length) return;
            window.dispatchEvent(new CustomEvent("env-paste-apply", {
                detail: {entries: this.parsed.entries, replace: this.mode === "replace"},
            }));
            this.close();
        },
    }));
});

window.envVarsEditorState = envVarsEditorState;
window.parseDotenv = parseDotenv;
