// A run this page streams or waits on is shown from the chat component's flags; otherwise the status is the
// server's, refreshed by each sessionSync fetch of the turns endpoint. Rename, the CI toggle and Archive write
// through the sessions API; rename and the CI toggle are only rendered for someone who may run on the repository.
document.addEventListener("alpine:init", () => {
  const WIDE = "(min-width: 1100px)";

  const OPEN_WATCH = ["watching", "fixing"];

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

  Alpine.data("sessionPage", (config) => ({
    latest: null,
    watch: null,
    runCount: 0,
    title: config.title || "",
    titleDraft: "",
    titleWidth: 0,
    editingTitle: false,
    archived: Boolean(config.archived),
    ciWatchBusy: false,
    ciWatchDisabled: "",
    ciWatchError: "",
    detailsOpen: false,
    wide: false,
    _settling: false,
    _watchAt: -Infinity,
    _media: null,
    _onMedia: null,
    init() {
      this.applyHeader(readJSON("session-header"));
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
        return localStorage.getItem(config.storageKey) === "open";
      } catch {
        return false;
      }
    },
    _remember(open) {
      try {
        localStorage.setItem(config.storageKey, open ? "open" : "closed");
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
    get _running() {
      return this.streaming || this.resuming || this._settling;
    },
    get busy() {
      return this._running || this.waiting;
    },
    get status() {
      if (this._running) return { variant: "running", label: config.labels.running };
      if (this.waiting) return { variant: "queued", label: config.labels.queued };
      return this.latest;
    },
    get retryUrl() {
      return this.busy ? "" : this.latest?.retry_url || "";
    },
    // Holds "running" from a run's end until sessionSync's next fetch brings its settled status, rather than
    // flashing the previous run's.
    settle() {
      this._settling = true;
    },
    applyHeader(data) {
      this._settling = false;
      if (!data || data.latest_run === undefined) return;
      this.latest = data.latest_run;
      this.runCount = data.run_count ?? this.runCount;
      this._takeWatch(data, data.requestedAt);
    },
    // A response asked for before the last toggle answered read the watch from before it; one without `watch` has
    // no merge request on record, so the page keeps its own.
    _takeWatch(data, requestedAt) {
      if (!("watch" in data) || requestedAt <= this._watchAt) return;
      this.watch = data.watch ?? null;
    },
    _setWatch(watch) {
      this.watch = watch ?? null;
      this._watchAt = performance.now();
    },
    async _send(url, method, body) {
      return fetch(url, {
        method,
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": this.csrfToken },
        body: JSON.stringify(body),
      });
    },
    // The input takes the title's width, so the bar does not jump when it swaps in.
    startRename() {
      this.titleDraft = this.title;
      this.titleWidth = this.$refs.titleButton?.getBoundingClientRect().width || 0;
      this.editingTitle = true;
      this.$nextTick(() => {
        this.$refs.titleInput?.focus();
        this.$refs.titleInput?.select();
      });
    },
    cancelRename() {
      this.editingTitle = false;
      this.$nextTick(() => this.$refs.titleButton?.focus());
    },
    // Enter and the blur that follows both land here, so only the first one saves.
    async commitRename() {
      if (!this.editingTitle) return;
      this.editingTitle = false;
      const next = this.titleDraft.trim();
      if (!next || next === this.title) return;
      const previous = this.title;
      this._showTitle(next);
      try {
        const res = await this._send(config.sessionUrl, "PATCH", { title: next });
        if (!res.ok) throw new Error(`rename failed: ${res.status}`);
        this._showTitle((await res.json()).title);
      } catch {
        this._showTitle(previous);
      }
    },
    _showTitle(title) {
      this.title = title;
      document.title = `${title} — ${config.titleSuffix}`;
    },
    // A green, exhausted or unreadable watch is over: ticking the item again starts a new one.
    get ciWatchOn() {
      return OPEN_WATCH.includes(this.watch?.state);
    },
    get ciWatchHint() {
      if (this.ciWatchDisabled) return this.ciWatchDisabled;
      if (this.ciWatchError) return this.ciWatchError;
      if (!this.watch) return config.labels.watchOff;
      if (this.ciWatchOn) return config.labels.watching.replace("{state}", this.watch.label);
      return this.watch.label;
    },
    async toggleCiWatch(mergeRequestIid) {
      if (this.ciWatchBusy || this.ciWatchDisabled) return;
      this.ciWatchBusy = true;
      this.ciWatchError = "";
      try {
        const res = await this._send(config.ciWatchUrl, "POST", {
          on: !this.ciWatchOn,
          merge_request_iid: mergeRequestIid,
          seen_state: this.watch?.state || "off",
        });
        const data = await res.json();
        if (res.status === 409 && data.reason) {
          this._setWatch(data.watch);
          if (data.reason === "disabled") this.ciWatchDisabled = data.detail;
          return;
        }
        if (!res.ok) throw new Error(`CI watch toggle failed: ${res.status}`);
        this._setWatch(data);
      } catch {
        this.ciWatchError = config.labels.watchFailed;
      } finally {
        this.ciWatchBusy = false;
      }
    },
    askArchive() {
      document.getElementById("session-archive-dialog")?.showModal();
    },
    async setArchived(archived) {
      const previous = this.archived;
      this.archived = archived;
      try {
        const res = await this._send(config.archiveUrl, "POST", { archived });
        if (!res.ok) throw new Error(`archive failed: ${res.status}`);
        this.archived = (await res.json()).archived;
      } catch {
        this.archived = previous;
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
