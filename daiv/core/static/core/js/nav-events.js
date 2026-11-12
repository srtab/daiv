/**
 * Live counters for the dashboard shell — unread notifications, running runs.
 *
 * One SSE connection per tab. Nothing here replaces DOM — the badges bind to this
 * store — so an open menu survives every update.
 *
 * A store rather than a component because the counts are read from several places in
 * the shell — both sidebar copies (persistent and mobile sheet) and the mobile top bar.
 * Seeded from the server-rendered counts so the first paint has no flash, then replaced
 * wholesale by each `snapshot` frame — the stream sends state, not deltas, so a
 * reconnect needs no replay.
 *
 *     <div x-data="..." x-init="$store.nav.start({ url, state })">
 *
 * `state` carries the server's own snapshot keys (`unread_count`, `running_runs`) rather than
 * the store's field names, so the page seed and the SSE frames are one shape read by one
 * function — a key renamed on the Python side then breaks both, not just the stream.
 *
 * On a session page `url` carries `?session=<thread_id>`, and each `session` frame the stream
 * then sends is re-announced to the page as a `daiv:session-changed` window event.
 *
 * A frame that moves the running snapshot (`running_runs` or `latest_running_start`) is
 * announced as `daiv:runs-changed`, and so is every reopen after the tab was hidden, since
 * runs may have started and finished unseen. The page seed never is: it carries no start,
 * so the first frame's start is only recorded. The sidebar's lists refetch on it.
 */
document.addEventListener("alpine:init", () => {
  Alpine.store("nav", {
    unread: 0,
    running: 0,
    _url: "",
    _source: null,
    _ended: false,
    _opened: false,
    _latestStart: null,

    /** Seed from the page render and open the stream. Idempotent per tab. */
    start({ url, state }) {
      this._applyState(state);
      if (this._url || !url || !window.EventSource) return;
      this._url = url;
      // Gated on `document.visibilityState`: an ungated stream would hold a worker, a DB
      // recount per poke and one of the browser's six per-origin connections per hidden tab.
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
      if (this._opened) this._announceRuns();
      this._opened = true;
      this._source = new EventSource(this._url);
      this._source.addEventListener("snapshot", (event) => this._apply(event.data));
      this._source.addEventListener("session", (event) => this._announceSession(event.data));
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
      const moved = this._runsMoved(snapshot);
      this._applyState(snapshot);
      if (moved) this._announceRuns();
    },

    _runsMoved(state) {
      if (!state) return false;
      const count = state.running_runs;
      const start = state.latest_running_start;
      return (
        (typeof count === "number" && count !== this.running) ||
        (typeof start === "string" && this._latestStart !== null && start !== this._latestStart)
      );
    },

    _announceRuns() {
      window.dispatchEvent(new CustomEvent("daiv:runs-changed"));
    },

    _announceSession(data) {
      let revision;
      try {
        revision = JSON.parse(data)?.revision;
      } catch {
        console.warn("nav: unreadable session frame");
        return;
      }
      if (!Number.isInteger(revision)) {
        console.warn("nav: session frame without an integer revision");
        return;
      }
      window.dispatchEvent(new CustomEvent("daiv:session-changed", { detail: { revision } }));
    },

    /** The only place the wire keys are read, so the seed and the stream can't drift. */
    _applyState(state) {
      if (!state) return;
      if (typeof state.unread_count === "number") this.unread = state.unread_count;
      if (typeof state.running_runs === "number") this.running = state.running_runs;
      if (typeof state.latest_running_start === "string") this._latestStart = state.latest_running_start;
    },
  });
});
