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
    Alpine.data("promptBox", ({
        initialSlug = "",
        initialRef = "",
        initialUseMax = false,
        maxRepos = 1,
        repoPickerUrl = "",
        branchPickerTemplate = "",
    }) => ({
        repos: initialSlug ? [{ slug: initialSlug, ref: initialRef || "" }] : [],
        useMax: initialUseMax,
        maxRepos,
        repoPickerUrl,
        branchPickerTemplate,

        popover: null,
        editingIndex: null,

        openRepoPicker(index = null) {
            this.editingIndex = index;
            this.popover = "repo";
            this.$nextTick(() => this._refresh(this.$refs.repoSearch));
        },

        openBranchPicker(index) {
            const repo = this.repos[index];
            if (!repo) return;
            this.editingIndex = index;
            this.popover = "branch";
            // Slug goes into a <path:slug> Django converter that accepts '/', so we leave the
            // separator unencoded — nginx's default `allow_encoded_slashes off` would 404 on %2F.
            const url =
                this.branchPickerTemplate.replace("__SLUG__", repo.slug) +
                "?selected=" +
                encodeURIComponent(repo.ref || "");
            this.$nextTick(() => {
                const input = this.$refs.branchSearch;
                if (!input) return;
                input.setAttribute("hx-get", url);
                // HTMX caches parsed hx-* attributes at processing time — re-process so the
                // new URL is picked up instead of the `__SLUG__` placeholder.
                window.htmx.process(input);
                this._refresh(input);
            });
        },

        _refresh(input) {
            if (!input) return;
            input.value = "";
            window.htmx.trigger(input, "refresh");
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
