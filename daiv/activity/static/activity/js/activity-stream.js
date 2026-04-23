/**
 * Alpine.js components for real-time activity status updates via SSE.
 *
 * activityStream (list page) — tracks multiple activities in place:
 *   dotClass(id, fallback)    → "status-dot-{variant}" CSS class
 *   statusClass(id, fallback) → "status-badge-{variant}" CSS class
 *   statusLabel(id, fallback) → human-readable label
 *
 * activityDetail (detail page) — subscribes to one activity and reloads the
 * page on any state change so server-rendered fields (started_at, finished_at,
 * elapsed counter, duration, timeline dots) reflect the new state.
 */
document.addEventListener("alpine:init", () => {
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
            return "status-badge-" + statusVariantFor(this.updates[id]?.status || fallback);
        },
        dotClass(id, fallback) {
            return "status-dot-" + statusVariantFor(this.updates[id]?.status || fallback);
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
