/**
 * Alpine component: editable env-vars list for the sandbox environment form.
 *
 * Owns the `rows` array only — the form template binds it to a hidden
 * `env_vars_json` input via `:value="JSON.stringify(rows)"` (see
 * `_form_body.html`). Registered globally so the HTMX-loaded drawer fragment
 * picks up the directive on re-render.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("envVarsEditor", (initial = []) => ({
        rows: Array.isArray(initial) ? initial : [],
    }));
});
