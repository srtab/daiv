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
 *     <div x-data="..." x-init="$store.nav.start({ url, state, runningLabel })">
 *
 * `state` carries the server's own snapshot keys (`unread_count`, `running_runs`) rather
 * than the store's field names, so the page seed and the SSE frames are one shape read by
 * one function — a key renamed on the Python side then breaks both, not just the stream.
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
    start({ url, state, runningLabel }) {
      this._applyState(state);
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
      this._applyState(snapshot);
    },

    /** The only place the wire keys are read, so the seed and the stream can't drift. */
    _applyState(state) {
      if (!state) return;
      if (typeof state.unread_count === "number") this.unread = state.unread_count;
      if (typeof state.running_runs === "number") this.running = state.running_runs;
    },
  });
});
