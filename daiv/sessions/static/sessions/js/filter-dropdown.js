/** The contract sessions/_filter_dropdown.html binds to, spread into a filter bar that has `open` and `_apply()`. */
window.filterDropdown = {
    _item(key) {
        const value = this[key];
        if (!value) return null;
        return this.$root.querySelector(`[data-filter-key="${key}"][data-value="${CSS.escape(value)}"]`);
    },
    toggle(key) {
        this.open = this.open === key ? null : key;
    },
    close(key, wrapper) {
        if (this.open !== key) return;
        this.open = null;
        wrapper?.querySelector(".filter-control__toggle").focus();
    },
    pick(key, value) {
        this[key] = value;
        this._apply();
    },
    labelFor(key) {
        return this._item(key)?.dataset.label ?? this[key];
    },
    leadFor(key) {
        return this._item(key)?.querySelector(".filter-menu-item__lead")?.innerHTML ?? "";
    },
    loadFailed(loader) {
        loader.querySelector(".filter-menu__loading").textContent = loader.dataset.errorMessage;
    },
};
