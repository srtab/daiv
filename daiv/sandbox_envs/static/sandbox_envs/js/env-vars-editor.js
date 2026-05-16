/**
 * Alpine component: editable env-vars list for the sandbox environment form.
 *
 * Adds live name validation (regex from sandbox_envs.models._ENV_VAR_NAME_RE)
 * and exposes a paste-overlay setter for Task 9's overlay component.
 */
const ENV_VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

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
});

window.envVarsEditorState = envVarsEditorState;
