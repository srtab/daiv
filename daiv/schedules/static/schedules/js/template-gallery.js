/**
 * Alpine component: side-drawer gallery for picking a schedule template.
 *
 * Templates are JSON-serialized server-side into `#schedule-templates-data`;
 * this component reads that on init, filters by query, and warns before
 * navigating away if the host form has unsaved edits (window.__scheduleFormDirty).
 *
 * Usage:
 *   <div x-data="templateGallery({ confirmMessage: '...' })" @open-template-gallery.window="open = true">
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("templateGallery", ({ confirmMessage = "" } = {}) => ({
        open: false,
        query: "",
        templates: JSON.parse(document.getElementById("schedule-templates-data").textContent),

        get filtered() {
            const q = this.query.trim().toLowerCase();
            if (!q) return this.templates;
            return this.templates.filter((t) => {
                const repoIds = (t.repos || []).map((r) => r.repo_id).join(" ");
                return (t.name + " " + t.description + " " + repoIds).toLowerCase().includes(q);
            });
        },

        close() {
            this.open = false;
            this.query = "";
        },

        confirmApply(ev) {
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return;
            if (window.__scheduleFormDirty && !window.confirm(confirmMessage)) {
                ev.preventDefault();
            }
        },
    }));
});
