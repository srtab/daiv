/**
 * A `.session-row` links through the stretched `<a>` it starts with; a `.session-row__hint` sits above that link so
 * its `title` shows, and a click on it opens the link, in a new tab on a middle or modified click.
 */
for (const type of ["click", "auxclick"]) {
    document.addEventListener(type, (event) => {
        const hint = event.target.closest(".session-row__hint");
        if (!hint || event.button > 1 || event.target.closest("a, button")) return;
        const link = hint.closest(".session-row")?.querySelector(":scope > a");
        if (!link) return;
        if (event.button === 1 || event.ctrlKey || event.metaKey || event.shiftKey) window.open(link.href, "_blank");
        else window.location.assign(link.href);
    });
}
