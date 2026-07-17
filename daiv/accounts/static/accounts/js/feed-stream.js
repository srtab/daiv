/**
 * Feed-scoped SSE consumer (Story 2.3, AC9) — live-resolves "classifying…" Feed items.
 *
 * Separate from ``sessionStream`` (session-stream.js), which hard-codes ``#session-in-flight``
 * and streams Run.status. This reads ``#feed-in-flight`` (terminal-but-unclassified run ids — the
 * INVERSE of the sessions predicate) and connects to the shared stream with ``?feed_ids=``. On an
 * ``{"envelope":"resolved"}`` frame it re-fetches the resolved item partial from ``feed_item`` and
 * swaps it outerHTML, then prunes that id from the in-flight collector so it stops streaming.
 *
 * Args: streamUrl (session_stream), itemUrlTemplate (feed_item URL with the placeholder uuid the
 * frame's id is substituted into). SSE unavailable → items resolve on next page load; the */15
 * reclassify cron backstops stranded runs. No polling fallback.
 */
document.addEventListener("alpine:init", () => {
    const PLACEHOLDER = "00000000-0000-0000-0000-000000000000";

    Alpine.data("feedStream", (streamUrl, itemUrlTemplate) => ({
        _source: null,
        // Bound so a server that keeps timing out with pending work can't spin up an
        // endless chain of re-subscriptions (mirrors sessionStream).
        _reconnects: 0,
        _maxReconnects: 3,
        init() {
            this._arm();
        },
        destroy() {
            if (this._source) this._source.close();
        },
        reconnect() {
            // Re-arm after the console body is HTMX-swapped: read the fresh in-flight ids.
            this._arm();
        },
        _arm() {
            if (this._source) {
                this._source.close();
                this._source = null;
            }
            this._reconnects = 0;
            const el = document.getElementById("feed-in-flight");
            if (!el) return;
            // Empty data-ids is correct silence: no classifying items to resolve.
            const ids = el.dataset.ids;
            if (ids) this._connect(streamUrl + "?feed_ids=" + ids);
        },
        _connect(url) {
            const source = new EventSource(url);
            this._source = source;
            source.onmessage = (event) => {
                let data;
                try {
                    data = JSON.parse(event.data);
                } catch (e) {
                    console.warn("feedStream: ignoring malformed SSE frame", e);
                    return;
                }
                if (data.done) {
                    source.close();
                    // Timed out (server MAX_DURATION) with items still pending — re-subscribe.
                    if (data.complete === false && this._reconnects < this._maxReconnects) {
                        this._reconnects += 1;
                        this._connect(url);
                    }
                    return;
                }
                if (data.envelope === "resolved" && data.id) {
                    this._resolve(data.id, itemUrlTemplate);
                }
            };
            source.onerror = () => {
                // Let EventSource auto-reconnect on transient drops; warn once permanently closed.
                if (source.readyState === EventSource.CLOSED) {
                    console.warn("feedStream: SSE connection closed; live Feed updates stopped");
                }
            };
        },
        _resolve(id, itemUrlTemplate) {
            const target = document.getElementById("feed-item-" + id);
            if (target && window.htmx) {
                // The re-fetched partial renders the resolved envelope and omits the classifying
                // hooks, so the item stops streaming.
                window.htmx.ajax("GET", itemUrlTemplate.replace(PLACEHOLDER, id), {
                    target: "#feed-item-" + id,
                    swap: "outerHTML",
                });
            }
            // Prune the resolved id so a stream re-subscribe doesn't re-emit it.
            const flight = document.getElementById("feed-in-flight");
            if (flight) {
                const remaining = (flight.dataset.ids || "").split(",").filter((x) => x && x !== id);
                flight.dataset.ids = remaining.join(",");
            }
        },
    }));
});
