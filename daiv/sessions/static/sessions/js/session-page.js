// A run this page streams or waits on is shown from the chat component's flags; otherwise the status is the
// server's, refreshed by each turns poll and once a run ends.
document.addEventListener("alpine:init", () => {
  const WIDE = "(min-width: 1100px)";

  const readJSON = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch {
      return null;
    }
  };

  // x-show reveals on the next animation frame, so a scroll sent from $nextTick can land on a hidden panel.
  const whenShown = (el, then, frames = 10) => {
    if (el.getClientRects().length) then();
    else if (frames > 0) (document.hidden ? setTimeout : requestAnimationFrame)(() => whenShown(el, then, frames - 1));
  };

  Alpine.data("sessionPage", ({ turnsUrl, storageKey, labels, runCount }) => ({
    latest: null,
    watch: null,
    runCount,
    detailsOpen: false,
    wide: false,
    _settling: false,
    _seq: 0,
    _media: null,
    _onMedia: null,
    init() {
      this.latest = readJSON("session-latest-run");
      this.watch = readJSON("session-watch");
      this._media = window.matchMedia?.(WIDE) ?? null;
      this.wide = Boolean(this._media?.matches);
      this._onMedia = (event) => {
        this.wide = event.matches;
        this.detailsOpen = this.wide && this._remembered();
      };
      this._media?.addEventListener?.("change", this._onMedia);
      this.detailsOpen = this.wide && this._remembered();
    },
    destroy() {
      this._media?.removeEventListener?.("change", this._onMedia);
    },
    _remembered() {
      try {
        return localStorage.getItem(storageKey) === "open";
      } catch {
        return false;
      }
    },
    _remember(open) {
      try {
        localStorage.setItem(storageKey, open ? "open" : "closed");
      } catch {
        // Blocked storage only means the panel starts closed next time.
      }
    },
    // Only the column's state is remembered: the sheet below `WIDE` is opened for one look and dismissed.
    setDetails(open) {
      this.detailsOpen = open;
      if (this.wide) this._remember(open);
    },
    toggleDetails() {
      this.setDetails(!this.detailsOpen);
    },
    openDetails(section) {
      this.setDetails(true);
      const el = document.getElementById(`session-details-${section}`);
      if (el) whenShown(el, () => el.scrollIntoView({ block: "start" }));
    },
    closeSheetDetails() {
      if (!this.wide) this.detailsOpen = false;
    },
    get status() {
      if (this.streaming || this.resuming || this._settling) return { variant: "running", label: labels.running };
      if (this.waiting) return { variant: "queued", label: labels.queued };
      return this.latest;
    },
    get retryUrl() {
      if (this.streaming || this.resuming || this.waiting || this._settling) return "";
      return this.latest?.retry_url || "";
    },
    applyPoll(data) {
      if (!data || data.latest_run === undefined) return;
      this._seq += 1;
      this._settling = false;
      this.latest = data.latest_run;
      this.watch = data.watch ?? null;
      this.runCount = data.run_count ?? this.runCount;
    },
    // Holds "running" until the run's settled status arrives, rather than flashing the previous run's.
    async refresh() {
      const token = ++this._seq;
      this._settling = true;
      try {
        const res = await fetch(turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        if (token !== this._seq) return;
        this.latest = data.latest_run ?? null;
        this.watch = data.watch ?? null;
        this.runCount = data.run_count ?? this.runCount;
      } catch {
        // The next poll or run end refreshes it again; a stale status is not worth an error.
      } finally {
        if (token === this._seq) this._settling = false;
      }
    },
    async copyLink(url) {
      try {
        await navigator.clipboard.writeText(new URL(url, window.location.href).href);
      } catch {
        // Clipboard access denied: the link stays in the menu's href for a manual copy.
      }
    },
  }));
});
