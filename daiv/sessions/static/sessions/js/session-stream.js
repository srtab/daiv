/**
 * Alpine.js component for real-time session/run status updates via SSE.
 *
 * sessionStream (list page) — tracks multiple runs in place:
 *   dotClass(id, fallback)    → object toggling status-dot-{variant} classes
 *   statusClass(id, fallback) → object toggling status-badge-{variant} classes
 *   statusLabel(id, fallback) → human-readable label
 *
 * Object class maps (rather than a single string) are required so Alpine
 * removes the previously rendered variant class when the status transitions —
 * otherwise the static server-rendered class lingers alongside the new one
 * and the later CSS rule wins.
 */
document.addEventListener("alpine:init", () => {
    const VARIANTS = ["success", "failed", "running", "queued", "pending"];

    function statusVariantFor(status) {
        if (status === "SUCCESSFUL") return "success";
        if (status === "FAILED") return "failed";
        if (status === "RUNNING") return "running";
        if (status === "QUEUED") return "queued";
        return "pending";
    }

    function statusLabelFor(status) {
        if (status === "SUCCESSFUL") return "Successful";
        if (status === "FAILED") return "Failed";
        if (status === "RUNNING") return "Running";
        if (status === "QUEUED") return "Queued";
        return "Pending";
    }

    function variantClassMap(prefix, active) {
        return Object.fromEntries(VARIANTS.map((v) => [prefix + v, v === active]));
    }

    Alpine.data("sessionStream", (streamUrl, inFlightIds) => ({
        updates: {},
        init() {
            if (!inFlightIds) return;
            const url = streamUrl + "?ids=" + inFlightIds;
            const source = new EventSource(url);
            source.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.done) {
                    source.close();
                    return;
                }
                this.updates[data.id] = data;
            };
            source.onerror = () => source.close();
        },
        statusClass(id, fallback) {
            return variantClassMap("status-badge-", statusVariantFor(this.updates[id]?.status || fallback));
        },
        dotClass(id, fallback) {
            return variantClassMap("status-dot-", statusVariantFor(this.updates[id]?.status || fallback));
        },
        statusLabel(id, fallback) {
            const update = this.updates[id];
            return update ? statusLabelFor(update.status) : fallback;
        },
    }));
});
