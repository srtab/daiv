// Polls the session turns endpoint while a background (non-chat) run holds the
// session slot, so the transcript grows as the run progresses.
document.addEventListener("alpine:init", () => {
  Alpine.data("sessionSync", ({ turnsUrl, active }) => ({
    active,
    _timer: null,
    init() {
      if (this.active) this._timer = setInterval(() => this.poll(), 5000);
    },
    destroy() {
      if (this._timer) clearInterval(this._timer);
    },
    async poll() {
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        window.dispatchEvent(new CustomEvent("daiv:session-turns", { detail: data }));
        if (!data.active) {
          clearInterval(this._timer);
          location.reload();
        }
      } catch (e) {
        /* transient network errors: keep polling */
      }
    },
  }));
});
