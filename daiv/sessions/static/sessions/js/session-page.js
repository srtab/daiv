// The session page's top bar and details panel: the latest run's status and Retry link, the CI watch, and whether
// the panel is open. A run this page streams or waits on is shown from the chat component's flags; otherwise the
// status is the server's, refreshed by each turns poll and once a run ends. Rename, the CI toggle and Archive write
// through the sessions API; their controls are only rendered for someone who may run on the repository.
document.addEventListener("alpine:init", () => {
  // The panel is a column from this width and a bottom sheet below it, the width the composer sheets switch at.
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

  Alpine.data("sessionPage", (config) => ({
    latest: null,
    watch: null,
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
      this.$nextTick(() => document.getElementById(`session-details-${section}`)?.scrollIntoView({ block: "start" }));
    },
    closeSheetDetails() {
      if (!this.wide) this.detailsOpen = false;
    },
    get status() {
      if (this.streaming || this.resuming || this._settling) return { variant: "running", label: config.labels.running };
      if (this.waiting) return { variant: "queued", label: config.labels.queued };
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
    },
    // Holds "running" until the run's settled status arrives, rather than flashing the previous run's.
    async refresh() {
      const token = ++this._seq;
      this._settling = true;
      try {
        const res = await fetch(config.turnsUrl, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        if (token !== this._seq) return;
        this.latest = data.latest_run ?? null;
        this.watch = data.watch ?? null;
      } catch {
        // The next poll or run end refreshes it again; a stale status is not worth an error.
      } finally {
        if (token === this._seq) this._settling = false;
      }
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
    get ciWatchHint() {
      if (this.ciWatchDisabled) return this.ciWatchDisabled;
      if (this.ciWatchError) return this.ciWatchError;
      if (!this.watch) return config.labels.watchOff;
      if (["watching", "fixing"].includes(this.watch.state)) {
        return config.labels.watching.replace("{state}", this.watch.label);
      }
      return this.watch.label;
    },
    async toggleCiWatch() {
      if (this.ciWatchBusy || this.ciWatchDisabled) return;
      this.ciWatchBusy = true;
      this.ciWatchError = "";
      try {
        const res = await this._send(config.ciWatchUrl, "POST", { on: !this.watch });
        if (res.status === 409) {
          const data = await res.json();
          this.watch = data.watch ?? null;
          if (data.reason === "disabled") this.ciWatchDisabled = data.detail;
          return;
        }
        if (!res.ok) throw new Error(`CI watch toggle failed: ${res.status}`);
        this.watch = (await res.json()) ?? null;
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
