// Polls the session turns endpoint while a background (non-chat) run holds the
// session slot, so the transcript grows as the run progresses.
// A run that starts after the page rendered (a delegation resume, a CI fix) moves the nav
// store's latest running start; the dormant poller then asks whether it is this session's.
document.addEventListener("alpine:init", () => {
  Alpine.data("sessionSync", ({ turnsUrl, active }) => ({
    active,
    _timer: null,
    _checking: false,
    _failures: 0,
    // Stop polling after this many consecutive failures so a persistent error
    // (endpoint 500s, auth expired) doesn't poll forever in the background.
    _maxFailures: 5,
    init() {
      if (this.active) this._start();
      this.$watch("$store.nav?.latestRunningStart", (latest) => {
        if (latest) this._checkForBackgroundRun();
      });
    },
    destroy() {
      if (this._timer) clearInterval(this._timer);
    },
    _start() {
      this._timer = setInterval(() => this.poll(), 5000);
    },
    _stop() {
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
      if (this._timer || this._checking) return;
      this._checking = true;
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        if (!data.background || this._timer) return;
        window.dispatchEvent(new CustomEvent("daiv:session-turns", { detail: data }));
        this._start();
      } catch (e) {
        // A failed check only delays the update: the next run that starts asks again.
      } finally {
        this._checking = false;
      }
    },
    async poll() {
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) {
          this._onFailure(`HTTP ${res.status}`);
          return;
        }
        const data = await res.json();
        this._failures = 0;
        window.dispatchEvent(new CustomEvent("daiv:session-turns", { detail: data }));
        if (!data.active) {
          this._stop();
          location.reload();
        }
      } catch (e) {
        // Transient network errors: keep polling until the failure cap is hit.
        this._onFailure(e && e.message ? e.message : "network error");
      }
    },
  }));
});
