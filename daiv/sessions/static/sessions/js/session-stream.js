/**
 * Alpine.js component for real-time session/run status updates via SSE.
 *
 * sessionStream (list page) — tracks multiple runs in place:
 *   dotClass(id, fallback)    → object toggling status-dot-{variant} classes, invisible unless running or waiting
 *   rowClass(id, fallback)    → object toggling the row's session-row--failed edge
 *   statusClass(id, fallback) → object toggling status-badge-{variant} classes
 *   statusLabel(id, fallback) → human-readable label
 *
 * A frame that changes the status a row shows dispatches `sessions:status-changed` on window.
 *
 * Object class maps (rather than a single string) are required so Alpine
 * removes the previously rendered variant class when the status transitions —
 * otherwise the static server-rendered class lingers alongside the new one
 * and the later CSS rule wins.
 */
document.addEventListener("alpine:init", () => {
    const VARIANTS = ["success", "failed", "running", "queued", "waiting", "pending"];

    function statusVariantFor(status) {
        if (status === "SUCCESSFUL") return "success";
        if (status === "FAILED") return "failed";
        if (status === "RUNNING") return "running";
        if (status === "QUEUED") return "queued";
        if (status === "WAITING_INPUT") return "waiting";
        return "pending";
    }

    function statusLabelFor(status) {
        if (status === "SUCCESSFUL") return "Done";
        if (status === "FAILED") return "Failed";
        if (status === "RUNNING") return "Running";
        if (status === "QUEUED") return "Queued";
        if (status === "WAITING_INPUT") return "Needs input";
        return "Pending";
    }

    function variantClassMap(prefix, active) {
        return Object.fromEntries(VARIANTS.map((v) => [prefix + v, v === active]));
    }

    Alpine.data("sessionStream", (streamUrl, inFlightIds) => ({
        updates: {},
        _source: null,
        // Bound so a server that keeps timing out with pending work can't spin
        // up an endless chain of re-subscriptions.
        _reconnects: 0,
        _maxReconnects: 3,
        init() {
            if (!inFlightIds) return;
            this._connect(streamUrl + "?ids=" + inFlightIds);
        },
        destroy() {
            if (this._source) this._source.close();
        },
        reconnect() {
            // After a results swap the in-flight set changes; the freshly rendered rows
            // already show the correct status (server-rendered fallback), so this only
            // restores LIVE updates for the new page. Ids are read from the swapped fragment.
            const el = document.getElementById("session-in-flight");
            this.updates = {};
            if (this._source) this._source.close();
            this._source = null;
            // Reset the reconnect budget: `_maxReconnects` caps stream-timeout retries WITHIN
            // one page, not across page swaps, so each swapped page starts with a fresh budget.
            this._reconnects = 0;
            if (!el) {
                // The fragment always renders this marker; its absence means the swap landed
                // something other than the results fragment (error page / redirect / template
                // drift). Warn instead of silently dropping live updates so it's debuggable.
                console.warn("sessionStream: #session-in-flight missing after swap; live updates not re-armed");
                return;
            }
            // Empty data-ids is correct silence: no non-terminal runs on this page to track.
            const ids = el.dataset.ids;
            if (ids) this._connect(streamUrl + "?ids=" + ids);
        },
        _connect(url) {
            const source = new EventSource(url);
            this._source = source;
            source.onmessage = (event) => {
                let data;
                try {
                    data = JSON.parse(event.data);
                } catch (e) {
                    console.warn("sessionStream: ignoring malformed SSE frame", e);
                    return;
                }
                if (data.done) {
                    source.close();
                    // The stream timed out (server MAX_DURATION) with runs still in
                    // flight — re-subscribe so badges don't freeze on stale state.
                    if (data.complete === false && this._reconnects < this._maxReconnects) {
                        this._reconnects += 1;
                        this._connect(url);
                    }
                    return;
                }
                const row = document.querySelector(`[data-run-id="${data.id}"]`);
                const before = this.updates[data.id]?.status ?? row?.dataset.runStatus;
                this.updates[data.id] = data;
                if (row && before !== data.status) {
                    window.dispatchEvent(new CustomEvent("sessions:status-changed", { detail: { id: data.id, status: data.status } }));
                }
            };
            source.onerror = () => {
                // Let EventSource auto-reconnect on transient drops; only warn once
                // the browser has permanently closed the connection.
                if (source.readyState === EventSource.CLOSED) {
                    console.warn("sessionStream: SSE connection closed; live status updates stopped");
                }
            };
        },
        statusClass(id, fallback) {
            return variantClassMap("status-badge-", statusVariantFor(this.updates[id]?.status || fallback));
        },
        dotClass(id, fallback) {
            const variant = statusVariantFor(this.updates[id]?.status || fallback);
            return { ...variantClassMap("status-dot-", variant), invisible: variant !== "running" && variant !== "waiting" };
        },
        rowClass(id, fallback) {
            return { "session-row--failed": (this.updates[id]?.status || fallback) === "FAILED" };
        },
        statusLabel(id, fallback) {
            const update = this.updates[id];
            return update ? statusLabelFor(update.status) : fallback;
        },
    }));
});
