/**
 * Live counters for the dashboard shell — unread notifications, running runs.
 *
 * One SSE connection per tab replaces what used to be a 10s HTMX poll of the whole
 * bell fragment. That poll is also what closed an open dropdown: it swapped
 * `#notifications-bell` by `outerHTML`, which tore down the Alpine component holding
 * `open`. Nothing here replaces DOM — the badges bind to this store — so the dropdown
 * survives every update.
 *
 * A store rather than a component because three elements read the same two numbers
 * (the bell badge, and the sidebar's running badge, which renders twice: desktop rail
 * plus mobile drawer). Seeded from the server-rendered counts so the first paint has
 * no flash, then replaced wholesale by each `snapshot` frame — the stream sends state,
 * not deltas, so a reconnect needs no replay.
 *
 *     <div x-data="..." x-init="$store.nav.start({ url, unread, running, runningLabel })">
 *
 * `runningLabel` is the sidebar badge's translated sentence with `%(count)s` still in it:
 * a live number can't go through `blocktranslate`, and keeping the whole sentence as one
 * msgid (rather than "3" + a translated word) leaves the translator in control of word
 * order. base_app.html passes it because it is what includes the sidebar.
 */
document.addEventListener("alpine:init", () => {
  Alpine.store("nav", {
    unread: 0,
    running: 0,
    _source: null,
    _runningLabel: "",

    get runningLabel() {
      return this._runningLabel.replace("%(count)s", this.running);
    },

    /** Seed from the page render and open the stream. Idempotent per tab. */
    start({ url, unread, running, runningLabel }) {
      this.unread = Number(unread) || 0;
      this.running = Number(running) || 0;
      this._runningLabel = runningLabel || "";
      if (this._source || !url || !window.EventSource) return;
      this._source = new EventSource(url);
      this._source.addEventListener("snapshot", (event) => this._apply(event.data));
      // The server sends `end` only when it has given up (a stream-level failure), so
      // reconnecting would just hammer a broken backend. The badges keep their last
      // values until the next page load.
      this._source.addEventListener("end", () => this.stop());
    },

    stop() {
      if (this._source) this._source.close();
      this._source = null;
    },

    _apply(data) {
      let snapshot;
      try {
        snapshot = JSON.parse(data);
      } catch {
        console.warn("nav: unreadable snapshot frame");
        return;
      }
      if (typeof snapshot.unread_count === "number") this.unread = snapshot.unread_count;
      if (typeof snapshot.running_runs === "number") this.running = snapshot.running_runs;
    },
  });
});
