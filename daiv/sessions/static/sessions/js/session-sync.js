// Fetches the session's turns when the nav stream announces a revision this page has not applied or a run stream
// ends, and hands each live run to the chat; it polls only while a run goes on that no stream shows, or without a bus.
document.addEventListener("alpine:init", () => {
  const FIRST_RETRY_MS = 2000;
  const MAX_RETRY_MS = 30000;
  const POLL_MS = 5000;
  const HEALTHY_STREAM_MS = 10000;

  // The one part of the transcript a run streamed to this tab never carries.
  const inboxEntries = (turns) => JSON.stringify((turns ?? []).filter((turn) => turn?.role === "inbox_event"));

  Alpine.data("sessionSync", ({ turnsUrl, liveRunId, revision, expired, active, polls }) => ({
    _revision: revision,
    _expired: Boolean(expired),
    _polls: Boolean(polls),
    _polling: Boolean(polls && active),
    _pollDelay: POLL_MS,
    _pollTimer: null,
    _following: liveRunId || "",
    _followedAt: liveRunId ? performance.now() : -Infinity,
    _finished: new Set(),
    _keepTranscript: false,
    _kept: null,
    _retry: null,
    _retryTimer: null,
    _announced: revision,
    _fetching: false,
    _dirty: false,
    _forced: false,
    init() {
      if (this._polling) this._schedulePoll();
    },
    destroy() {
      clearTimeout(this._retryTimer);
      clearTimeout(this._pollTimer);
    },
    changed({ revision }) {
      this._announced = revision;
      if (revision !== this._revision) this.refresh();
    },
    runEnded({ runId, reason, own }) {
      this._following = "";
      this._keepTranscript = false;
      if (reason === "finished" || reason === "not_started") {
        if (reason === "finished") this._finished.add(runId);
        this._keepTranscript = Boolean(own);
        this._kept = null;
        this._retry = null;
      } else if (reason === "queued") {
        this._retry = null;
      } else {
        const healthy = performance.now() - this._followedAt >= HEALTHY_STREAM_MS;
        const first = healthy || this._retry?.runId !== runId;
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
        if (this._polling) this._schedulePoll(Math.min(this._pollDelay * 2, MAX_RETRY_MS));
        return;
      }
      this._revision = data.revision;
      // The expired banner and the composer it replaces are server-rendered, so only a reload swaps them.
      if (this._expired && data.expired_banner === false) {
        this._expired = false;
        location.reload();
        return;
      }
      this._apply({ ...data, requestedAt });
      this._polling = Boolean(this._unstreamed(data.live_run) || (this._polls && data.active));
      if (this._polling) this._schedulePoll();
      else clearTimeout(this._pollTimer);
    },
    _apply(data) {
      const seen = { runCount: data.run_count, inbox: inboxEntries(data.turns) };
      if (this._keepTranscript) this._kept ??= seen;
      const headerOnly =
        this._keepTranscript &&
        !this._unstreamed(data.live_run) &&
        seen.runCount === this._kept.runCount &&
        seen.inbox === this._kept.inbox;
      if (!headerOnly) this._keepTranscript = false;
      window.dispatchEvent(new CustomEvent(headerOnly ? "daiv:session-header" : "daiv:session-turns", { detail: data }));
      this._follow(data.live_run);
    },
    // A run whose stream ended as finished yet still holds the slot: it is about to let go, or its relay failed and it
    // carries on unpublished, so only fetching shows it.
    _unstreamed(run) {
      return Boolean(run && this._finished.has(run.id));
    },
    _schedulePoll(delay = POLL_MS) {
      clearTimeout(this._pollTimer);
      this._pollDelay = delay;
      this._pollTimer = setTimeout(() => {
        this._pollTimer = null;
        this.refresh();
      }, delay);
    },
    _follow(run) {
      if (run && run.id === this._following) return;
      clearTimeout(this._retryTimer);
      this._following = "";
      if (!run || this._finished.has(run.id)) return;
      this._following = run.id;
      this._keepTranscript = false;
      const handOver = () => {
        this._followedAt = performance.now();
        window.dispatchEvent(new CustomEvent("daiv:session-live-run", { detail: run }));
      };
      if (run.id === this._retry?.runId) this._retryTimer = setTimeout(handOver, this._retry.delay);
      else handOver();
    },
  }));
});
