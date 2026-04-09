/**
 * Shared Alpine.js components for real-time activity status updates via SSE.
 *
 * Usage (list page):
 *   <div x-data="activityStream('/stream/', '1,2,3')">
 *     <span :class="statusClass('1', 'RUNNING')" class="status-badge"
 *           x-text="statusLabel('1', 'RUNNING')"></span>
 *   </div>
 *
 * Usage (detail page):
 *   <div x-data="activityDetail('/stream/', '42')">
 *     <span :class="statusClass()" class="status-badge"
 *           x-text="statusLabel()"></span>
 *   </div>
 */
document.addEventListener("alpine:init", () => {
    function statusClassFor(status) {
        if (status === "SUCCESSFUL") return "status-badge-success";
        if (status === "FAILED") return "status-badge-failed";
        if (status === "RUNNING") return "status-badge-running";
        return "status-badge-pending";
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
            return statusClassFor(this.updates[id]?.status || fallback);
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
            return statusClassFor(this.currentStatus);
        },
        statusLabel() {
            return statusLabelFor(this.currentStatus);
        },
    }));
});
