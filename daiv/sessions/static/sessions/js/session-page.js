// Applies each turns fetch sessionSync announces and polls nothing; the status follows the chat while it streams or
// waits on a run. Rename, each merge request's CI toggle and Archive write through the sessions API. The details panel
// is always shown from `WIDE` up and is a sheet below it; its tab and folded sections are remembered per browser.
document.addEventListener("alpine:init", () => {
  const WIDE = "(min-width: 1100px)";
  const TABS = ["overview", "info"];
  const FOLDED = {
    repositories: false,
    environment: true,
    context: false,
    spend: true,
    outputs: true,
    references: false,
  };
  const CI_TONES_WORST_FIRST = ["fail", "found", "attn", "clear"];

  const stored = (key) => {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  };

  const store = (key, value) => {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Blocked storage only means the choice is forgotten on the next load.
    }
  };

  const storedFolds = (key) => {
    let saved;
    try {
      saved = JSON.parse(stored(key) ?? "{}");
    } catch {
      saved = null;
    }
    return Object.fromEntries(
      Object.entries(FOLDED).map(([section, folded]) => [
        section,
        typeof saved?.[section] === "boolean" ? saved[section] : folded,
      ]),
    );
  };

  const ciKey = (item) => `${item.repo_id}!${item.merge_request_iid}`;

  const readJSON = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch {
      return null;
    }
  };

  // ``upNext`` with the chat message its sender just queued, under the group of the run that will take it.
  const withProvisional = (upNext, provisional) => {
    const id = `inbox-${provisional.event_id}`;
    const groups = upNext?.groups ?? [];
    if (groups.some((group) => group.bubbles.some((bubble) => bubble.id === id))) return upNext;
    const bubble = {
      id,
      event_id: provisional.event_id,
      kind: "chat_message",
      received: false,
      failed: false,
      text: provisional.text,
      source_iid: null,
      trigger_type: "",
      web_url: "",
      created_at: provisional.created_at,
    };
    // A message with no run yet, or one the payload does not know, waits behind the inbox runs listed, before the jobs.
    const at = provisional.run_id ? groups.findIndex((group) => group.run_id === provisional.run_id) : -1;
    const own = { run_id: provisional.run_id, who: "", is_viewer: true, starting: false, bubbles: [bubble], told: [] };
    const jobs = groups.findIndex((group) => !group.starting && group.bubbles.some((b) => b.kind === "job"));
    const end = jobs < 0 ? groups.length : jobs;
    const next =
      at < 0
        ? [...groups.slice(0, end), own, ...groups.slice(end)]
        : groups.map((group, index) => (index === at ? { ...group, bubbles: [...group.bubbles, bubble] } : group));
    return { groups: next, told: upNext?.told ?? [] };
  };

  // x-show reveals on the next animation frame, so a scroll sent from $nextTick can land on a hidden panel.
  const whenShown = (el, then, frames = 10) => {
    if (el.getClientRects().length) then();
    else if (frames > 0) (document.hidden ? setTimeout : requestAnimationFrame)(() => whenShown(el, then, frames - 1));
  };

  Alpine.data("sessionPage", (config) => ({
    latest: null,
    // CI on each merge request the session owns.
    ci: [],
    // What the session will do next (``sessions.up_next``).
    upNext: null,
    runCount: 0,
    delegations: null,
    repositories: null,
    references: [],
    title: config.title || "",
    titleDraft: "",
    titleWidth: 0,
    editingTitle: false,
    ciWatchBusy: false,
    ciWatchDisabled: {},
    ciWatchError: {},
    detailsOpen: false,
    detailsTab: "overview",
    wide: false,
    _folds: { ...FOLDED },
    _settling: false,
    _watchAt: -Infinity,
    _archivedAt: -Infinity,
    _archiveSeq: 0,
    _media: null,
    _onMedia: null,
    _provisional: [],
    init() {
      this.applyHeader(readJSON("session-header"));
      const tab = stored(`${config.storageKey}.tab`);
      if (TABS.includes(tab)) this.detailsTab = tab;
      this._folds = storedFolds(`${config.storageKey}.folds`);
      this._media = window.matchMedia?.(WIDE) ?? null;
      this.wide = Boolean(this._media?.matches);
      this._onMedia = (event) => {
        this.wide = event.matches;
        this.detailsOpen = this.wide;
      };
      this._media?.addEventListener?.("change", this._onMedia);
      this.detailsOpen = this.wide;
    },
    destroy() {
      this._media?.removeEventListener?.("change", this._onMedia);
    },
    // The sender's queued message, shown until a payload asked for after it was sent, which the server built
    // knowing it, replaces it.
    addProvisional({ event_id, run_id, text }) {
      const provisional = { event_id, run_id, text, at: performance.now(), created_at: new Date().toISOString() };
      this._provisional.push(provisional);
      this.upNext = withProvisional(this.upNext, provisional);
    },
    upNextHeading(group, index) {
      const T = window.TranscriptLabels;
      const who = group.is_viewer ? T.t("up_next_you") : group.who;
      const step = index === 0 ? "up_next_next" : "up_next_then";
      const title = who ? T.t(step, { who }) : T.t(`${step}_anyone`);
      return [title, this._upNextWhen(group, index)].filter(Boolean).join(" · ");
    },
    _upNextWhen(group, index) {
      const T = window.TranscriptLabels;
      if (group.starting) return T.t("up_next_starting");
      if (index === 0) return this.busy ? T.t("up_next_after_live") : "";
      const previous = this.upNext.groups[index - 1];
      if (previous.is_viewer) return T.t("up_next_after_yours");
      return previous.who ? T.t("up_next_after_theirs", { who: previous.who }) : "";
    },
    _applyUpNext(upNext, requestedAt) {
      this._provisional = this._provisional.filter((provisional) => provisional.at >= requestedAt);
      this.upNext = this._provisional.reduce(withProvisional, upNext);
    },
    setDetails(open) {
      this.detailsOpen = open || this.wide;
    },
    selectTab(tab) {
      if (!TABS.includes(tab)) return;
      this.detailsTab = tab;
      store(`${config.storageKey}.tab`, tab);
    },
    // Arrow keys on the tab list move the selection and the focus along it, as a WAI-ARIA tablist does.
    cycleTab(tablist, step) {
      this.selectTab(TABS[(TABS.indexOf(this.detailsTab) + step + TABS.length) % TABS.length]);
      this.$nextTick(() => tablist.querySelector("[aria-selected=true]")?.focus());
    },
    isFolded(section) {
      return Boolean(this._folds[section]);
    },
    toggleFold(section) {
      if (!(section in FOLDED)) return;
      this._folds = { ...this._folds, [section]: !this._folds[section] };
      store(`${config.storageKey}.folds`, JSON.stringify(this._folds));
    },
    openDetails(section) {
      this.selectTab("overview");
      if (this.isFolded(section)) this.toggleFold(section);
      this.setDetails(true);
      const el = document.getElementById(`session-details-${section}`);
      if (el) whenShown(el, () => el.scrollIntoView({ block: "start" }));
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
    canRetry(turn) {
      return !this.busy && !this.archived && Boolean(this.latest?.retry) && turn.id === this.latest.marker_id;
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
      // A response asked for before the last toggle answered read CI from before it.
      const stale = data.requestedAt <= this._watchAt;
      if ("ci" in data && !stale) this._showCi(data);
      if ("up_next" in data) this._applyUpNext(data.up_next ?? null, data.requestedAt ?? -Infinity);
      if ("delegations" in data) this.delegations = data.delegations ?? null;
      if ("repositories" in data) this._applyRepositories(data.repositories ?? null);
      if ("references" in data) this.references = data.references ?? [];
      if ("archived" in data && !(data.requestedAt <= this._archivedAt)) this.archived = data.archived;
      if (data.title && !this.editingTitle) this._showTitle(data.title);
    },
    // An unchanged card is left alone, as rewriting its errors would have them announced again.
    _applyRepositories(next) {
      if (JSON.stringify(next) !== JSON.stringify(this.repositories)) this.repositories = next;
    },
    // The main repository's branch and merge request are the chat's, which the live stream keeps current.
    get repositoryRows() {
      const [main, ...extras] = this.repositories?.rows ?? [];
      if (!main) return [];
      const mr = this.thread?.merge_request;
      const live = mr
        ? { mr_url: mr.url || "", mr_iid: mr.id ?? null, mr_title: mr.title || "", mr_draft: Boolean(mr.draft) }
        : {};
      return [{ ...main, ref: this.thread?.ref || main.ref, ...live }, ...extras];
    },
    // The CI of every merge request the session owns in `row`'s repository, the row's own first.
    ciItems(row) {
      const own = (item) => item.merge_request_iid === row.mr_iid;
      return this.ci.filter((item) => item.repo_id === row.repo_id).sort((a, b) => own(b) - own(a));
    },
    // A finished run left this repository nothing to show: no merge request, change, CI or publish error.
    isQuiet(row) {
      return (
        Boolean(this.repositories?.ran) && !row.mr_iid && !row.diff_stats && !row.errors.length && !this.ciItems(row).length
      );
    },
    // Names the merge request of a CI item that is not the card's own.
    ciTag(row, item) {
      return item.merge_request_iid === row.mr_iid ? "" : `!${item.merge_request_iid}`;
    },
    ciDisabled(item) {
      return Boolean(this.ciWatchDisabled[ciKey(item)]);
    },
    get summary() {
      const rows = this.repositoryRows;
      const withMr = rows.filter((row) => row.mr_iid);
      const shown = rows.flatMap((row) => this.ciItems(row));
      const worst = CI_TONES_WORST_FIRST.map((tone) => shown.find((item) => item.tone === tone)).find(Boolean);
      return {
        main: rows[0]?.repo_id ?? "",
        more: Math.max(rows.length - 1, 0),
        mrs: withMr.length,
        mrIid: withMr.length === 1 ? withMr[0].mr_iid : null,
        mrLabel: withMr.length ? window.TranscriptLabels.tn("details_merge_requests", withMr.length) : "",
        tone: worst?.tone ?? "",
        ci: worst?.label ?? "",
      };
    },
    get summaryLabel() {
      const { main, more, mrIid, mrLabel, ci } = this.summary;
      const repositories = more ? `${main} +${more}` : main;
      const parts = [repositories, mrIid ? `!${mrIid}` : mrLabel, ci && `${config.labels.ci}: ${ci}`].filter(Boolean);
      return `${config.labels.summary}: ${parts.join(", ")}`;
    },
    get repositoriesHeading() {
      return this.repositoryRows.length === 1 ? config.labels.repository : config.labels.repositories;
    },
    // A lone repository's section is labelled in the singular, so its count would only repeat that.
    get repositoriesSummary() {
      const { more, mrLabel } = this.summary;
      if (!more) return mrLabel;
      return [String(more + 1), mrLabel].filter(Boolean).join(" · ");
    },
    _showCi(data) {
      this.ci = data?.ci ?? [];
    },
    _setCi(data) {
      this._showCi(data);
      this._watchAt = performance.now();
    },
    // The run that last looked at a failure, whose summary says what it found.
    ciExplanation(item) {
      return item?.status === "failing" && item.explanation_run_id ? `#run-status-${item.explanation_run_id}` : "";
    },
    ciText(item) {
      if (!item) return "";
      const jobs = item.status === "failing" ? (item.failed_jobs ?? []) : [];
      return [item.label, jobs.join(", ")].filter(Boolean).join(" · ");
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
    ciNote(item) {
      if (!item) return "";
      const key = ciKey(item);
      if (this.ciWatchDisabled[key]) return this.ciWatchDisabled[key];
      if (this.ciWatchError[key]) return this.ciWatchError[key];
      if (!item.watch_enabled) return config.labels.watchOff;
      return config.labels.watching.replace("{state}", item.label);
    },
    async toggleCiWatch(item) {
      const key = ciKey(item);
      if (this.ciWatchBusy || this.ciDisabled(item)) return;
      this.ciWatchBusy = true;
      this.ciWatchError = { ...this.ciWatchError, [key]: "" };
      try {
        const res = await this._send(config.ciWatchUrl, "POST", {
          on: !item.watch_enabled,
          merge_request_iid: item.merge_request_iid,
          repo_id: item.repo_id,
        });
        const data = await res.json();
        if (res.status === 409 && data.reason) {
          this._setCi(data);
          if (data.reason === "disabled") this.ciWatchDisabled = { ...this.ciWatchDisabled, [key]: data.detail };
          else this.ciWatchError = { ...this.ciWatchError, [key]: data.detail };
          return;
        }
        if (!res.ok) throw new Error(`CI watch toggle failed: ${res.status}`);
        this._setCi(data);
      } catch {
        this.ciWatchError = { ...this.ciWatchError, [key]: config.labels.watchFailed };
      } finally {
        this.ciWatchBusy = false;
      }
    },
    askArchive() {
      document.getElementById("session-archive-dialog")?.showModal();
    },
    // Only the latest of overlapping toggles restores on failure or reopens the page to fetched state.
    async setArchived(archived) {
      const previous = this.archived;
      const seq = ++this._archiveSeq;
      this.archived = archived;
      this._archivedAt = Infinity;
      try {
        const res = await this._send(config.archiveUrl, "POST", { archived });
        if (!res.ok) throw new Error(`archive failed: ${res.status}`);
      } catch {
        if (seq === this._archiveSeq) this.archived = previous;
      } finally {
        if (seq === this._archiveSeq) this._archivedAt = performance.now();
      }
    },
    async copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // Clipboard access denied: the text stays on the page for a manual copy.
      }
    },
    copyLink(url) {
      return this.copyText(new URL(url, window.location.href).href);
    },
  }));
});
