// Fetches the session's turns when the nav stream announces a revision this page has not applied or a run stream
// ends, and hands each live run to the chat; its one timer re-attaches to a run whose stream dropped, backing off.
document.addEventListener("alpine:init", () => {
  const FIRST_RETRY_MS = 2000;
  const MAX_RETRY_MS = 30000;

  Alpine.data("sessionSync", ({ turnsUrl, liveRunId, revision }) => ({
    _revision: revision,
    _following: liveRunId || "",
    _finished: new Set(),
    _keepTranscript: false,
    _keptRunCount: null,
    _retry: null,
    _retryTimer: null,
    _fetching: false,
    _dirty: false,
    destroy() {
      clearTimeout(this._retryTimer);
    },
    changed({ revision }) {
      if (revision !== this._revision) this.refresh();
    },
    runEnded({ runId, reason, own, frames }) {
      this._following = "";
      if (reason === "finished" || reason === "not_started") {
        if (reason === "finished") this._finished.add(runId);
        this._keepTranscript = Boolean(own);
        this._keptRunCount = null;
        this._retry = null;
      } else if (reason === "queued") {
        this._keepTranscript = false;
        this._retry = null;
      } else {
        this._keepTranscript = false;
        const first = frames > 0 || this._retry?.runId !== runId;
        const delay = first ? FIRST_RETRY_MS : Math.min(this._retry.delay * 2, MAX_RETRY_MS);
        this._retry = { runId, delay };
      }
      this.refresh();
    },
    async refresh() {
      if (this._fetching) {
        this._dirty = true;
        return;
      }
      this._fetching = true;
      try {
        do {
          this._dirty = false;
          await this._fetchTurns();
        } while (this._dirty);
      } finally {
        this._fetching = false;
      }
    },
    // `requestedAt` lets a listener tell a response from before its own write apart from one after it.
    async _fetchTurns() {
      const requestedAt = performance.now();
      let data;
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        data = await res.json();
      } catch {
        // A running run bumps no revision, so forgetting it lets the next announcement (even a reconnect's) refetch.
        // The empty header still releases the "running" the page holds from a run's end.
        this._revision = null;
        window.dispatchEvent(new CustomEvent("daiv:session-header", { detail: null }));
        return;
      }
      this._revision = data.revision;
      this._apply({ ...data, requestedAt });
    },
    _apply(data) {
      if (this._keepTranscript && this._keptRunCount === null) this._keptRunCount = data.run_count;
      const headerOnly = this._keepTranscript && data.run_count <= this._keptRunCount;
      if (!headerOnly) this._keepTranscript = false;
      window.dispatchEvent(new CustomEvent(headerOnly ? "daiv:session-header" : "daiv:session-turns", { detail: data }));
      this._follow(data.live_run);
    },
    _follow(run) {
      if (!run || this._finished.has(run.id) || run.id === this._following) return;
      this._following = run.id;
      this._keepTranscript = false;
      clearTimeout(this._retryTimer);
      const handOver = () => window.dispatchEvent(new CustomEvent("daiv:session-live-run", { detail: run }));
      if (run.id === this._retry?.runId) this._retryTimer = setTimeout(handOver, this._retry.delay);
      else handOver();
    },
  }));
});
