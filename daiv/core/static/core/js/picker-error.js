/**
 * Shared failure row for the HTMX-driven picker popovers (repo, branch, subscriber).
 *
 * HTMX skips the swap on 4xx/5xx, so without this a failed request leaves the loading
 * skeleton pulsing forever over a stale list. The message always comes from the nearest
 * `data-error-message` ancestor so it stays translatable in the template.
 */
window.pickerError = {
    row(el) {
        const li = document.createElement("li");
        li.className = "picker-popover__error";
        li.textContent = el.closest("[data-error-message]")?.dataset.errorMessage ?? "";
        return li;
    },

    showIn(list) {
        if (!list) return;
        const ul = document.createElement("ul");
        ul.appendChild(this.row(list));
        list.replaceChildren(ul);
    },
};
