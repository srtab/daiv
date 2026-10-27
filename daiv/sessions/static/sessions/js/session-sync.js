// Polls the session turns endpoint while no run streams to this page, and hands the
// session to the chat component as soon as a run holder appears.
// A page that rendered idle stays dormant until a run the viewer can see starts (the nav store's
// latest running start moves); it then asks whether that run is background work on this session.
document.addEventListener("alpine:init", () => {
  Alpine.data("sessionSync", ({ turnsUrl, liveRunId, active }) => ({
    _engaged: active,
    _followed: liveRunId || "",
    _timer: null,
    _checking: false,
    _failures: 0,
    _seq: 0,
    // Stop polling after this many consecutive failures so a persistent error
    // (endpoint 500s, auth expired) doesn't poll forever in the background.
    _maxFailures: 5,
    init() {
      if (this._engaged && !liveRunId) this._start();
      this.$watch("$store.nav?.latestRunningStart", (latest) => {
        if (latest) this._checkForBackgroundRun();
      });
    },
    destroy() {
      this._stop();
    },
    resume() {
      // A dormant page's own chat turn ending is not a reason to poll, and settling would reload it.
      if (!this._engaged) return this._refreshHeader();
      this._start();
      this.poll();
    },
    // One fetch for the top bar only, dispatched even on failure so the page stops holding "running".
    async _refreshHeader() {
      let data = null;
      const requestedAt = performance.now();
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (res.ok) data = await res.json();
      } catch (e) {
        // The next run end or poll refreshes it; a stale status is not worth an error.
      }
      window.dispatchEvent(new CustomEvent("daiv:session-header", { detail: data && { ...data, requestedAt } }));
    },
    _start() {
      if (!this._timer) this._timer = setInterval(() => this.poll(), 5000);
    },
    _stop() {
      this._seq += 1;
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
    },
    _onFailure(reason) {
      this._failures += 1;
      if (this._failures >= this._maxFailures) {
        console.warn(`sessionSync: stopping transcript polling after repeated failures (${reason}); reload to retry`);
        this._stop();
      }
    },
    async _checkForBackgroundRun() {
      if (this._engaged || this._checking) return;
      this._checking = true;
      try {
        const requestedAt = performance.now();
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        if (!data.background) return;
        this._engaged = true;
        this._start();
        this._apply(data, requestedAt);
      } catch (e) {
        // A failed check only delays the update: the next run that starts asks again.
      } finally {
        this._checking = false;
      }
    },
    async poll() {
      const token = ++this._seq;
      const requestedAt = performance.now();
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) {
          this._onFailure(`HTTP ${res.status}`);
          return;
        }
        const data = await res.json();
        if (token !== this._seq) return;
        this._apply(data, requestedAt);
      } catch (e) {
        // Transient network errors: keep polling until the failure cap is hit.
        this._onFailure(e && e.message ? e.message : "network error");
      }
    },
    // `requestedAt` lets a listener tell a response from before its own write apart from one after it.
    _apply(data, requestedAt) {
      this._failures = 0;
      window.dispatchEvent(new CustomEvent("daiv:session-turns", { detail: { ...data, requestedAt } }));
      if (!data.active) {
        this._stop();
        this._engaged = false;
        if (window.dispatchEvent(new CustomEvent("daiv:session-settled", { cancelable: true }))) location.reload();
      } else if (data.live_run && data.live_run.id !== this._followed) {
        this._stop();
        this._followed = data.live_run.id;
        window.dispatchEvent(new CustomEvent("daiv:session-live-run", { detail: data.live_run }));
      }
    },
  }));
});
