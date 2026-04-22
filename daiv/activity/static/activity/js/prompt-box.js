/**
 * Keeps the hidden <input> elements in sync with the visible chip row via
 * ``:value`` bindings; those hidden inputs carry the actual POST payload.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("promptBox", ({ initialSlug = "", initialRef = "", initialUseMax = false, maxRepos = 1 }) => ({
        repos: initialSlug ? [{ slug: initialSlug, ref: initialRef || "" }] : [],
        useMax: initialUseMax,
        maxRepos,
        mode: null, // 'add' | 'edit' | null
        editingIndex: null,
        draft: { slug: "", ref: "" },

        openEdit(index) {
            this.draft = { ...this.repos[index] };
            this.editingIndex = index;
            this.mode = "edit";
        },

        openAdd() {
            this.draft = { slug: "", ref: "" };
            this.editingIndex = null;
            this.mode = "add";
        },

        commit() {
            const slug = (this.draft.slug || "").trim();
            if (!slug) return;
            const entry = { slug, ref: (this.draft.ref || "").trim() };
            if (this.mode === "add") {
                this.repos.push(entry);
            } else if (this.mode === "edit" && this.editingIndex !== null) {
                this.repos.splice(this.editingIndex, 1, entry);
            }
            this.cancel();
        },

        cancel() {
            this.mode = null;
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
