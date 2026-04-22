/**
 * Thin Alpine state shell for the prompt box.
 *
 * Owns the chip list (`repos`), the use-max toggle, and the open/close state
 * of the HTMX-driven repo/branch pickers. The list contents themselves are
 * server-rendered into `#repo-picker-list` / `#branch-picker-list` and attach
 * back into this component's state via `@click="setRepo(...)"` / `setBranch(...)`
 * — Alpine's MutationObserver picks those directives up when HTMX swaps them in.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("promptBox", ({ initialSlug = "", initialRef = "", initialUseMax = false, maxRepos = 1 }) => ({
        repos: initialSlug ? [{ slug: initialSlug, ref: initialRef || "" }] : [],
        useMax: initialUseMax,
        maxRepos,

        popover: null,
        editingIndex: null,

        openRepoPicker(index = null) {
            this.editingIndex = index;
            this.popover = "repo";
        },

        openBranchPicker(index) {
            this.editingIndex = index;
            this.popover = "branch";
        },

        closePopover() {
            this.popover = null;
            this.editingIndex = null;
        },

        setRepo(slug, defaultBranch) {
            const entry = { slug, ref: defaultBranch || "" };
            if (this.editingIndex === null) this.repos.push(entry);
            else this.repos.splice(this.editingIndex, 1, entry);
            this.closePopover();
        },

        setBranch(ref) {
            if (this.editingIndex == null) return;
            this.repos[this.editingIndex].ref = ref;
            this.closePopover();
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
