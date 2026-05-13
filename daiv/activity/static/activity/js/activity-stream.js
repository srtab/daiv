/**
 * Alpine.js components for real-time activity status updates via SSE.
 *
 * activityStream (list page) — tracks multiple activities in place:
 *   dotClass(id, fallback)    → object toggling status-dot-{variant} classes
 *   statusClass(id, fallback) → object toggling status-badge-{variant} classes
 *   statusLabel(id, fallback) → human-readable label
 *
 * Object class maps (rather than a single string) are required so Alpine
 * removes the previously rendered variant class when the status transitions —
 * otherwise the static server-rendered class lingers alongside the new one
 * and the later CSS rule wins.
 *
 * activityDetail (detail page) — subscribes to one activity and reloads the
 * page on any state change so server-rendered fields (started_at, finished_at,
 * elapsed counter, duration, timeline dots) reflect the new state.
 */
document.addEventListener("alpine:init", () => {
    const VARIANTS = ["success", "failed", "running", "pending"];

    function statusVariantFor(status) {
        if (status === "SUCCESSFUL") return "success";
        if (status === "FAILED") return "failed";
        if (status === "RUNNING") return "running";
        return "pending";
    }

    function statusLabelFor(status) {
        if (status === "SUCCESSFUL") return "Successful";
        if (status === "FAILED") return "Failed";
        if (status === "RUNNING") return "Running";
        return "Pending";
    }

    function variantClassMap(prefix, active) {
        return Object.fromEntries(VARIANTS.map((v) => [prefix + v, v === active]));
    }

    Alpine.data("activityStream", (streamUrl, inFlightIds) => ({
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

    // The SSE endpoint always emits the current state on first poll (it doesn't
    // know what the page rendered with), so reload only when the status has
    // actually drifted from what the template saw — otherwise a RUNNING page
    // would reload every poll interval.
    Alpine.data("activityDetail", (streamUrl, activityId, initialStatus) => ({
        init() {
            const url = streamUrl + "?ids=" + activityId;
            const source = new EventSource(url);
            source.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.done || (data.status && data.status !== initialStatus)) {
                    source.close();
                    window.location.reload();
                }
            };
            source.onerror = () => source.close();
        },
    }));
});
