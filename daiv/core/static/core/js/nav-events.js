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
 * `runningLabel` is the sidebar badge's translated sentence with `{count}` still in it:
 * a live number can't go through `blocktranslate`, and keeping the whole sentence as one
 * msgid (rather than "3" + a translated word) leaves the translator in control of word
 * order. base_app.html passes it because it is what includes the sidebar.
 *
 * Brace-style, not `%(count)s`: `{% translate %}` doubles every `%` before the catalog
 * lookup, so a percent-style msgid misses and silently renders the untranslated source.
 */
document.addEventListener("alpine:init", () => {
  Alpine.store("nav", {
    unread: 0,
    running: 0,
    _url: "",
    _source: null,
    _ended: false,
    _runningLabel: "",

    get runningLabel() {
      return this._runningLabel.replace("{count}", this.running);
    },

    /** Seed from the page render and open the stream. Idempotent per tab. */
    start({ url, unread, running, runningLabel }) {
      this.unread = Number(unread) || 0;
      this.running = Number(running) || 0;
      this._runningLabel = runningLabel || "";
      if (this._url || !url || !window.EventSource) return;
      this._url = url;
      // The poll this replaced was gated on `document.visibilityState === 'visible'`;
      // an ungated stream would hold a worker, a DB recount per poke and one of the
      // browser's six per-origin connections for every backgrounded tab.
      document.addEventListener("visibilitychange", () => this._sync());
      this._sync();
    },

    /** Terminal: the server gave up, so reconnecting would hammer a broken backend. */
    stop() {
      this._ended = true;
      this._close();
    },

    _sync() {
      if (document.visibilityState === "hidden") this._close();
      else this._open();
    },

    _open() {
      if (this._source || this._ended || !this._url) return;
      this._source = new EventSource(this._url);
      this._source.addEventListener("snapshot", (event) => this._apply(event.data));
      this._source.addEventListener("end", () => this.stop());
    },

    _close() {
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
