// Polls the session turns endpoint while no run streams to this page, and hands the
// session to the chat component as soon as a run holder appears.
document.addEventListener("alpine:init", () => {
  Alpine.data("sessionSync", ({ turnsUrl, liveRunId }) => ({
    _followed: liveRunId || "",
    _timer: null,
    _failures: 0,
    _seq: 0,
    // Stop polling after this many consecutive failures so a persistent error
    // (endpoint 500s, auth expired) doesn't poll forever in the background.
    _maxFailures: 5,
    init() {
      this._onEnded = () => {
        this._start();
        this.poll();
      };
      window.addEventListener("daiv:live-run-ended", this._onEnded);
      if (!liveRunId) this._start();
    },
    destroy() {
      this._stop();
      window.removeEventListener("daiv:live-run-ended", this._onEnded);
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
    async poll() {
      const token = ++this._seq;
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) {
          this._onFailure(`HTTP ${res.status}`);
          return;
        }
        const data = await res.json();
        if (token !== this._seq) return;
        this._failures = 0;
        window.dispatchEvent(new CustomEvent("daiv:session-turns", { detail: data }));
        if (!data.active) {
          this._stop();
          if (window.dispatchEvent(new CustomEvent("daiv:session-settled", { cancelable: true }))) location.reload();
        } else if (data.live_run && data.live_run.id !== this._followed) {
          this._stop();
          this._followed = data.live_run.id;
          window.dispatchEvent(new CustomEvent("daiv:session-live-run", { detail: data.live_run }));
        }
      } catch (e) {
        // Transient network errors: keep polling until the failure cap is hit.
        this._onFailure(e && e.message ? e.message : "network error");
      }
    },
  }));
});
