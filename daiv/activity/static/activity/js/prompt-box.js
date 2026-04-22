/**
 * Alpine component for the agent-run prompt box.
 *
 * Owns the visible chip row, add/edit popover, Max toggle, and textarea
 * autosize. The hidden <input> elements in the partial carry the actual
 * POST payload; this component keeps their ``.value`` in sync via
 * ``:value`` bindings on the inputs.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("promptBox", ({ initialSlug = "", initialRef = "", initialUseMax = false, maxRepos = 1 }) => ({
        repos: initialSlug ? [{ slug: initialSlug, ref: initialRef || "" }] : [],
        useMax: initialUseMax,
        maxRepos,
        // null = closed, -1 = adding, 0..n-1 = editing that index
        editingIndex: null,
        draft: { slug: "", ref: "" },

        openEdit(index) {
            this.draft = { ...this.repos[index] };
            this.editingIndex = index;
        },

        openAdd() {
            this.draft = { slug: "", ref: "" };
            this.editingIndex = -1;
        },

        commit() {
            const slug = (this.draft.slug || "").trim();
            if (!slug) return;
            const entry = { slug, ref: (this.draft.ref || "").trim() };
            if (this.editingIndex === -1) {
                this.repos.push(entry);
            } else {
                this.repos.splice(this.editingIndex, 1, entry);
            }
            this.cancel();
        },

        cancel() {
            this.editingIndex = null;
            this.draft = { slug: "", ref: "" };
        },

        remove(index) {
            this.repos.splice(index, 1);
        },

        autosize(el) {
            el.style.height = "auto";
            el.style.height = el.scrollHeight + "px";
        },
    }));
});
