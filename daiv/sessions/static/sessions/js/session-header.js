// The session header's latest-run status and Retry link. A run this page streams or waits on is shown from the
// chat component's flags; otherwise the status is the server's, refreshed by each turns poll and once a run ends.
document.addEventListener("alpine:init", () => {
  Alpine.data("sessionHeader", ({ turnsUrl, labels }) => ({
    latest: null,
    _settling: false,
    _resize: null,
    _seq: 0,
    init() {
      const el = document.getElementById("session-header-latest-run");
      try {
        this.latest = el ? JSON.parse(el.textContent) : null;
      } catch {
        this.latest = null;
      }
      const column = this.$el.closest(".chat-column");
      if (column && typeof ResizeObserver !== "undefined") {
        this._resize = new ResizeObserver(() => {
          column.style.setProperty("--session-header-h", `${this.$el.offsetHeight}px`);
        });
        this._resize.observe(this.$el);
      }
    },
    destroy() {
      this._resize?.disconnect();
    },
    get _inFlight() {
      return Boolean(this.streaming || this.resuming || this.waiting);
    },
    get status() {
      if (this.streaming || this.resuming || this._settling) return { variant: "running", label: labels.running };
      if (this.waiting) return { variant: "queued", label: labels.queued };
      return this.latest;
    },
    get retryUrl() {
      return this._inFlight || this._settling ? "" : this.latest?.retry_url || "";
    },
    applyLatest(latest) {
      if (latest === undefined) return;
      this._seq += 1;
      this._settling = false;
      this.latest = latest;
    },
    // Holds "running" until the run's settled status arrives, rather than flashing the previous run's.
    async refresh() {
      const token = ++this._seq;
      this._settling = true;
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        if (token === this._seq) this.latest = data.latest_run ?? null;
      } catch {
        // The next poll or run end refreshes it again; a stale pill is not worth an error.
      } finally {
        if (token === this._seq) this._settling = false;
      }
    },
  }));
});
