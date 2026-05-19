/**
 * Alpine component: simple chip list of repo ids (owner/repo) bound to a
 * hidden JSON input named `repo_ids_json`.
 *
 * Constructor arg: Array<string> — initial repo ids (already deduped).
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("repoIdsEditor", (initial = []) => ({
        ids: Array.isArray(initial) ? [...initial] : [],
        draft: "",
        error: "",

        addDraft() {
            const value = this.draft.trim();
            if (!value) return;
            if (!/^[^\s/]+\/[^\s/]+$/.test(value)) {
                this.error = "Use 'owner/repo' format.";
                return;
            }
            if (this.ids.includes(value)) {
                this.error = "Already added.";
                return;
            }
            this.ids = [...this.ids, value];
            this.draft = "";
            this.error = "";
        },

        removeAt(i) {
            this.ids = this.ids.filter((_, idx) => idx !== i);
        },
    }));
});
