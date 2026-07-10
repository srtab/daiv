// Polls the session turns endpoint while a background (non-chat) run holds the
// session slot, so the transcript grows as the run progresses.
document.addEventListener("alpine:init", () => {
  Alpine.data("sessionSync", ({ turnsUrl, active }) => ({
    active,
    _timer: null,
    _failures: 0,
    // Stop polling after this many consecutive failures so a persistent error
    // (endpoint 500s, auth expired) doesn't poll forever in the background.
    _maxFailures: 5,
    init() {
      if (this.active) this._timer = setInterval(() => this.poll(), 5000);
    },
    destroy() {
      if (this._timer) clearInterval(this._timer);
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
