/**
 * Alpine helper: wire SortableJS to a built-in and a custom row container.
 *
 * Reorder is within-section only — built-in rows can't be dragged into
 * the custom list or vice versa. After a drop, the helper rewrites every
 * row's hidden `*-sort_order` input to `100 + index * 10` in DOM order so
 * the next form submit persists the new order.
 */
document.addEventListener("alpine:init", () => {
    Alpine.magic("providerReorder", () => (builtInEl, customEl) => {
        if (typeof Sortable === "undefined") {
            console.warn("SortableJS not loaded — provider reorder disabled");
            return;
        }
        const rewriteOrder = (container) => {
            container.querySelectorAll('[data-row]').forEach((row, idx) => {
                const input = row.querySelector('input[name$="-sort_order"]');
                if (input) input.value = String(100 + idx * 10);
            });
        };
        for (const container of [builtInEl, customEl]) {
            if (!container) continue;
            Sortable.create(container, {
                handle: "[data-drag-handle]",
                animation: 150,
                ghostClass: "opacity-40",
                onEnd: () => rewriteOrder(container),
            });
        }
    });
});
