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
        initialRepos = [],
        initialUseMax = false,
        maxRepos = 1,
        repoPickerUrl = "",
        branchPickerTemplate = "",
        conflictMessageTemplate = "Repository already in the list: __LABEL__.",
    }) => ({
        repos: (initialRepos || []).map(r => ({ slug: r.repo_id, ref: r.ref || "" })),
        useMax: initialUseMax,
        maxRepos,
        repoPickerUrl,
        branchPickerTemplate,
        conflictMessageTemplate,

        popover: null,
        editingIndex: null,
        repoLoading: false,
        branchLoading: false,
        conflictIndex: null,
        _conflictTimer: null,

        init() {
            this.$el.addEventListener("htmx:beforeRequest", (e) => {
                if (e.target === this.$refs.repoSearch) this.repoLoading = true;
                if (e.target === this.$refs.branchSearch) this.branchLoading = true;
            });
            this.$el.addEventListener("htmx:afterSwap", (e) => {
                if (e.target === this.$refs.repoPickerList) this.repoLoading = false;
                if (e.target === this.$refs.branchPickerList) this.branchLoading = false;
            });
            this.$el.addEventListener("htmx:sendError", (e) => {
                if (e.target === this.$refs.repoSearch) this.repoLoading = false;
                if (e.target === this.$refs.branchSearch) this.branchLoading = false;
            });
        },

        destroy() {
            if (this._conflictTimer) clearTimeout(this._conflictTimer);
        },

        get conflictMessage() {
            const repo = this.conflictIndex === null ? null : this.repos[this.conflictIndex];
            if (!repo) return "";
            const label = repo.ref ? `${repo.slug} on ${repo.ref}` : repo.slug;
            return this.conflictMessageTemplate.replace("__LABEL__", label);
        },

        openRepoPicker(index = null) {
            this.editingIndex = index;
            this.repoLoading = true;
            this.popover = "repo";
            this.$nextTick(() => this._refresh(this.$refs.repoSearch));
        },

        openBranchPicker(index) {
            const repo = this.repos[index];
            if (!repo) return;
            this.editingIndex = index;
            this.branchLoading = true;
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
            const ref = defaultBranch || "";
            const conflict = this._findConflict(slug, ref, this.editingIndex);
            if (conflict !== -1) {
                this._flagConflict(conflict);
                this.closePopover();
                return;
            }
            const entry = { slug, ref };
            if (this.editingIndex === null) this.repos.push(entry);
            else this.repos.splice(this.editingIndex, 1, entry);
            this.closePopover();
        },

        setBranch(ref) {
            if (this.editingIndex == null) return;
            const repo = this.repos[this.editingIndex];
            const conflict = this._findConflict(repo.slug, ref, this.editingIndex);
            if (conflict !== -1) {
                this._flagConflict(conflict);
                this.closePopover();
                return;
            }
            this.repos[this.editingIndex].ref = ref;
            this.closePopover();
        },

        remove(index) {
            this.repos.splice(index, 1);
            if (this.conflictIndex !== null) {
                if (this.conflictIndex === index) this._clearConflict();
                else if (index < this.conflictIndex) this.conflictIndex -= 1;
            }
            if (this.editingIndex !== null) {
                if (this.editingIndex === index) this.closePopover();
                else if (index < this.editingIndex) this.editingIndex -= 1;
            }
        },

        _findConflict(slug, ref, skipIndex) {
            return this.repos.findIndex(
                (r, i) => i !== skipIndex && r.slug === slug && (r.ref || "") === (ref || ""),
            );
        },

        _flagConflict(index) {
            this.conflictIndex = index;
            if (this._conflictTimer) clearTimeout(this._conflictTimer);
            this._conflictTimer = setTimeout(() => this._clearConflict(), 3000);
        },

        _clearConflict() {
            this.conflictIndex = null;
            this._conflictTimer = null;
        },

        autosize(el) {
            el.style.height = "auto";
            el.style.height = el.scrollHeight + "px";
        },
    }));
});
