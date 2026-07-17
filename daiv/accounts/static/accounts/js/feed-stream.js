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
        _streamUrl() {
            // Build the stream URL from the CURRENT in-flight ids (resolved ids are pruned as they
            // land), or null when there is nothing to stream.
            const el = document.getElementById("feed-in-flight");
            if (!el) return null;
            const ids = el.dataset.ids;
            return ids ? streamUrl + "?feed_ids=" + ids : null;
        },
        _arm() {
            if (this._source) {
                this._source.close();
                this._source = null;
            }
            this._reconnects = 0;
            const url = this._streamUrl();
            if (url) this._connect(url);
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
                    // Timed out (server MAX_DURATION) with items still pending — re-subscribe using
                    // the CURRENT in-flight ids (resolved ones already pruned; newly-classifying ones
                    // picked up), not the stale connect-time URL. The bound stops an endless chain.
                    if (data.complete === false && this._reconnects < this._maxReconnects) {
                        this._reconnects += 1;
                        const next = this._streamUrl();
                        if (next) this._connect(next);
                    }
                    return;
                }
                if (data.envelope === "resolved" && data.id) {
                    this._resolve(data.id, itemUrlTemplate);
                }
            };
            source.onerror = () => {
                if (source.readyState === EventSource.CLOSED) {
                    console.warn("feedStream: SSE connection closed; live Feed updates stopped");
                }
            };
        },
        _resolve(id, itemUrlTemplate) {
            const target = document.getElementById("feed-item-" + id);
            // Only consume the resolution once we can actually swap the item; otherwise leave it
            // in-flight so a reconnect or full reload retries it (the */15 reclassify cron backstops).
            if (!(target && window.htmx)) return;
            window.htmx.ajax("GET", itemUrlTemplate.replace(PLACEHOLDER, id), {
                target: "#feed-item-" + id,
                swap: "outerHTML",
            });
            // Prune the resolved id so a re-subscribe doesn't re-request it.
            const flight = document.getElementById("feed-in-flight");
            if (flight) {
                const remaining = (flight.dataset.ids || "").split(",").filter((x) => x && x !== id);
                flight.dataset.ids = remaining.join(",");
            }
        },
    }));
});
