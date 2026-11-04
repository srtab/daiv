// Fetches the session's turns when the nav stream announces a revision this page has not applied or a run stream
// ends, and hands each live run to the chat; its one timer re-attaches to a run whose stream dropped, backing off.
document.addEventListener("alpine:init", () => {
  const FIRST_RETRY_MS = 2000;
  const MAX_RETRY_MS = 30000;

  Alpine.data("sessionSync", ({ turnsUrl, liveRunId, revision, expired }) => ({
    _revision: revision,
    _expired: Boolean(expired),
    _following: liveRunId || "",
    _finished: new Set(),
    _keepTranscript: false,
    _keptRunCount: null,
    _retry: null,
    _retryTimer: null,
    _announced: revision,
    _fetching: false,
    _dirty: false,
    _forced: false,
    destroy() {
      clearTimeout(this._retryTimer);
    },
    changed({ revision }) {
      this._announced = revision;
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
      this.refresh({ force: true });
    },
    // A change announced during a fetch costs one more unless the response already carries it; a run's end may not
    // have bumped the revision yet, so it always does.
    async refresh({ force = false } = {}) {
      if (this._fetching) {
        this._dirty = true;
        this._forced ||= force;
        return;
      }
      this._fetching = true;
      try {
        do {
          this._dirty = false;
          this._forced = false;
          await this._fetchTurns();
        } while (this._forced || (this._dirty && this._revision !== this._announced));
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
      // The expired banner and the composer it replaces are server-rendered, so only a reload swaps them.
      if (this._expired && data.expired === false) {
        this._expired = false;
        location.reload();
        return;
      }
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
