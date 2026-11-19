/** The contract sessions/_filter_dropdown.html binds to, spread into a filter bar that has `open` and `_apply()`. */
window.filterDropdown = {
    _item(key) {
        const value = this.value(key);
        if (!value) return null;
        return this.$root.querySelector(`[data-filter-key="${key}"][data-value="${CSS.escape(value)}"]`);
    },
    toggle(key) {
        this.open = this.open === key ? null : key;
    },
    value(key) {
        return this[key];
    },
    pick(key, value) {
        this[key] = value;
        this._apply();
    },
    labelFor(key) {
        return this._item(key)?.dataset.label ?? this.value(key);
    },
    leadFor(key) {
        return this._item(key)?.querySelector(".filter-menu-item__lead")?.innerHTML ?? "";
    },
};
