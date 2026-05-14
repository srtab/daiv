document.addEventListener("alpine:init", () => {
    Alpine.magic("providerReorder", () => (builtInEl, customEl) => {
        if (typeof Sortable === "undefined") {
            console.warn("SortableJS not loaded — provider reorder disabled");
            for (const handle of document.querySelectorAll("[data-drag-handle]")) {
                handle.classList.add("hidden");
            }
            return;
        }
        const rewriteOrder = (container) => {
            container.querySelectorAll("[data-row]").forEach((row, idx) => {
                const input = row.querySelector('input[name$="-sort_order"]');
                if (input) {
                    input.value = String(100 + idx * 10);
                } else {
                    console.error("provider row missing sort_order input", row);
                }
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
