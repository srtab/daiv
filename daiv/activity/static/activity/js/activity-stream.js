/**
 * Alpine.js components for real-time activity status updates via SSE.
 *
 * activityStream (list page) — tracks multiple activities:
 *   dotClass(id, fallback)    → "status-dot-{variant}" CSS class
 *   statusClass(id, fallback) → "status-badge-{variant}" CSS class
 *   statusLabel(id, fallback) → human-readable label
 *
 * activityDetail (detail page) — tracks one activity, reloads on completion:
 *   statusClass() → "status-badge-{variant}" CSS class
 *   statusLabel() → human-readable label
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
            return statusLabelFor(this.updates[id]?.status || fallback);
        },
    }));

    Alpine.data("activityDetail", (streamUrl, activityId) => ({
        currentStatus: null,
        init() {
            const url = streamUrl + "?ids=" + activityId;
            const source = new EventSource(url);
            source.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.done) {
                    source.close();
                    window.location.reload();
                    return;
                }
                this.currentStatus = data.status;
                if (data.status === "SUCCESSFUL" || data.status === "FAILED") {
                    source.close();
                    window.location.reload();
                }
            };
            source.onerror = () => source.close();
        },
        statusClass() {
            return "status-badge-" + statusVariantFor(this.currentStatus);
        },
        statusLabel() {
            return statusLabelFor(this.currentStatus);
        },
    }));
});
