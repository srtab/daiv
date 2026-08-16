(() => {
  const AGUI = {
    RUN_STARTED: "RUN_STARTED",
    RUN_FINISHED: "RUN_FINISHED",
    RUN_ERROR: "RUN_ERROR",
    TEXT_MESSAGE_START: "TEXT_MESSAGE_START",
    TEXT_MESSAGE_CONTENT: "TEXT_MESSAGE_CONTENT",
    TEXT_MESSAGE_END: "TEXT_MESSAGE_END",
    TEXT_MESSAGE_CHUNK: "TEXT_MESSAGE_CHUNK",
    TOOL_CALL_START: "TOOL_CALL_START",
    TOOL_CALL_ARGS: "TOOL_CALL_ARGS",
    TOOL_CALL_END: "TOOL_CALL_END",
    TOOL_CALL_RESULT: "TOOL_CALL_RESULT",
    REASONING_START: "REASONING_START",
    REASONING_MESSAGE_CONTENT: "REASONING_MESSAGE_CONTENT",
    REASONING_END: "REASONING_END",
    STATE_SNAPSHOT: "STATE_SNAPSHOT",
    CUSTOM: "CUSTOM",
  };

  // Normalize a raw GitState.merge_request snapshot (snake_case Pydantic dump)
  // into the shape the composer pill expects. Mirrors server-side
  // ``chat.repo_state.mr_to_payload`` — keep both in sync.
  const normalizeStateMr = (raw) => {
    if (!raw || typeof raw !== "object") return null;
    return {
      id: raw.merge_request_id ?? null,
      url: raw.web_url ?? null,
      title: raw.title ?? null,
      draft: Boolean(raw.draft),
      source_branch: raw.source_branch ?? null,
      target_branch: raw.target_branch ?? null,
    };
  };

  // Structured-response tool names emitted by the diff_to_metadata subagents.
  // We render their TOOL_CALL_* lifecycle as compact phase chips ("Creating
  // merge request…" / "Committing changes…") instead of letting the raw JSON
  // structured response surface as a tool card. Subagent text + reasoning are
  // already silenced server-side via `emit-messages: false`, so this is the
  // only signal the chat sees from the publish pipeline before the post-publish
  // STATE_SNAPSHOT carrying the new ``merge_request`` lands.
  const PUBLISH_PHASE_TOOLS = {
    PullRequestMetadata: { label: "Creating merge request" },
    CommitMetadata: { label: "Committing changes" },
  };

  // Reads a Django ``json_script`` payload. Returns `fallback` when the element is
  // absent or its content isn't valid JSON, so a missing block degrades rather than
  // taking the whole component down at init.
  const loadJSONScript = (id, fallback) => {
    const el = document.getElementById(id);
    if (!el) return fallback;
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      console.error("chat: failed to parse %s", id, err);
      return fallback;
    }
  };

  const loadInitialMergeRequest = () => {
    const v = loadJSONScript("chat-initial-merge-request", null);
    return v && typeof v === "object" ? v : null;
  };

  // ``{lines_added, lines_removed, files_changed}`` as the publisher measured it — never
  // summed client-side from edit tool calls, which double-counts repeat edits and scores
  // bash-driven changes as zero.
  const normalizeDiffStats = (raw) => {
    if (!raw || typeof raw !== "object") return null;
    const added = Number(raw.lines_added);
    const removed = Number(raw.lines_removed);
    if (!Number.isFinite(added) || !Number.isFinite(removed)) return null;
    return { added, removed };
  };

  const loadInitialDiffStats = () => normalizeDiffStats(loadJSONScript("chat-initial-diff-stats", null));

  // Tools whose args directly name a file the agent *modified*. Read-only tools
  // (read_file, grep, glob, ls) and search patterns don't qualify — they're
  // noise in the "files touched" rail. Bash-driven mutations (rm, mv, scripts,
  // find -delete, …) are folded in from the bash tool's `files_changed` result.
  const PATH_TOOLS = new Set(["write_file", "edit_file"]);

  // Where this is missing, `autosize()` polyfills growth and the `:placeholder-shown`
  // floor in input.css covers the empty box, whose wrapped placeholder JS can't measure.
  const NATIVE_FIELD_SIZING = CSS.supports("field-sizing", "content");

  const uuid = () => crypto.randomUUID();

  const HTTP_ERROR_MESSAGES = {
    403: "You don't have access to this conversation.",
    404: "Conversation not found.",
    409: "Another run is already in progress for this thread. Wait for it to finish, or try again.",
  };

  const formatHttpError = async (resp) => {
    const friendly = HTTP_ERROR_MESSAGES[resp.status];
    if (friendly) return friendly;
    try {
      const data = await resp.clone().json();
      if (data?.detail) return data.detail;
    } catch {
      /* fall through */
    }
    return `Request failed (status ${resp.status}). Please retry.`;
  };

  const loadInitialTurns = () => loadJSONScript("chat-initial-turns", []);

  // Every MCP server this user could load, one row per server, `is_default` marking the
  // ``active`` ones. Rows the health check already knows are broken carry
  // `available: false`: they stay in the selection (so the thread keeps them once the
  // outage is fixed) but their switch is inert.
  const loadMcpCatalog = () => {
    const rows = loadJSONScript("chat-mcp-servers", []);
    return Array.isArray(rows) ? rows : [];
  };

  // The thread's stored overrides, already resolved against the live pool server-side.
  const loadMcpSelection = () => {
    const stored = loadJSONScript("chat-mcp-selected", null);
    return Array.isArray(stored) ? stored.filter((n) => typeof n === "string") : null;
  };

  // Menu-while-token form of the backend bare-command parser (slash_commands/parser.py):
  // a space or newline after the token breaks the match and closes the menu.
  const SLASH_TOKEN_RE = /^\s*\/([\w-]*)$/;

  const loadSlashCatalog = () => {
    const rows = loadJSONScript("chat-slash-commands", []);
    return Array.isArray(rows) ? rows : [];
  };

  // English fallbacks for the fragments the composer assembles counts from. The page
  // passes translated ones through ``chat({labels: …})``; these only apply if it doesn't.
  const DEFAULT_LABELS = {
    file: "file",
    files: "files",
    stopped: "stopped",
    tool: "tool",
    tools: "tools",
    filteredTo: "filtered to",
    notSynced: "not synced yet",
    mcpServers: "MCP servers",
  };

  const THINKING_LABELS = [
    "Thinking…",
    "Reading files…",
    "Exploring the codebase…",
    "Understanding context…",
    "Planning the change…",
    "Running tools…",
  ];

  // Tiers mirror the ``duration`` template filter (sessions/templatetags/session_tags.py)
  // so a run reads the same live as it does once it lands in the sessions list.
  const formatElapsed = (sec) => {
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ${sec % 60}s`;
    return `${Math.floor(min / 60)}h ${min % 60}m`;
  };

  const pickPath = (argsStr) => {
    try {
      const args = JSON.parse(argsStr);
      if (!args || typeof args !== "object") return null;
      return args.path ?? args.file_path ?? null;
    } catch {
      return null;
    }
  };

  const bashFilesChanged = (resultStr) =>
    (window.parseBashSuccess ? window.parseBashSuccess(resultStr) : null)?.files_changed ?? [];

  // Only consider write_todos calls from the current ask. Walking backwards and bailing at
  // the most recent user turn clears the rail on follow-up, so stale "all complete" lists
  // from a finished run don't linger.
  const findLatestTodos = (turns) => {
    for (let i = turns.length - 1; i >= 0; i--) {
      const turn = turns[i];
      if (turn.role === "run_status") continue;
      if (turn.role === "user") return [];
      for (let j = turn.segments.length - 1; j >= 0; j--) {
        const s = turn.segments[j];
        if (s.type === "tool_call" && s.name === "write_todos") {
          try {
            const args = JSON.parse(s.args || "{}");
            return Array.isArray(args.todos) ? args.todos : [];
          } catch {
            return [];
          }
        }
      }
    }
    return [];
  };

  // These caches live here, not on the returned data object: Alpine makes every own
  // property reactive, so a cache read inside the getter would subscribe each reader to it
  // and the release below would re-trigger them all, forever. Per-instance — Alpine calls
  // this factory once per component.
  const chat = (config) => {
  let filesCache = null;
  let todosCache = null;
  return {
    endpoint: config.endpoint,
    streamEndpoint: config.streamEndpoint || "",
    cancelEndpoint: config.cancelEndpoint || "",
    csrfToken: config.csrfToken || "",
    // Hydrate the MR pill from the server-rendered checkpoint. We rebuild the
    // thread object so Alpine tracks `merge_request` as a reactive property
    // from the first render — assigning a *new* key onto an existing reactive
    // proxy after init doesn't always re-render templates.
    thread: config.thread ? { ...config.thread, merge_request: loadInitialMergeRequest() } : null,
    selectedSandboxEnvId: config.selectedSandboxEnvId || "",
    // Locked-pill labels for the composer chips. Server-side ``_composer.html`` renders
    // the static text at template time, which on a brand-new chat is the empty-thread
    // fallback ("Pick a model" / "Auto") — by the time those pills become visible
    // (``x-show="thread"``) the user has already picked something the server hasn't seen
    // yet. The locked pills read these via ``x-text`` so the picker keeps its selection
    // visible during the hero→composer transition, with no page refresh needed.
    // Refreshed reactively by the ``daiv:agent-changed`` / ``daiv:env-changed`` listeners
    // (see chat_detail.html). The agent picker dispatches its own pillLabel so the locked
    // pill stays in sync without re-deriving the label from the raw model spec.
    lockedAgentLabel: config.initialAgentLabel || "",
    lockedEnvLabel: config.initialEnvLabel || "",
    lockedEnvScope: config.initialEnvScope || "",
    // Current agent-picker selection, kept in sync via ``daiv:agent-changed``. Forwarded
    // to the server on submit; empty when the user hasn't touched the picker yet (the
    // first event fires only on actual change). Submit() then falls back to reading the
    // picker's hidden inputs so a no-touch submit still carries the seeded spec.
    _agentModel: "",
    _agentThinkingLevel: "",
    // Server-translated "Auto" so re-picking Auto after a real env reverts the
    // locked pill text correctly (the JS itself has no i18n surface).
    _envAutoLabel: config.envAutoLabel || "Auto",
    turns: loadInitialTurns(),
    // Server-measured diff for the work published so far, refreshed by every
    // STATE_SNAPSHOT that carries it. Drives the ``+x −y`` progress pill.
    diffStats: loadInitialDiffStats(),
    draftMessage: "",
    draftRepoId: "",
    draftRef: "",
    streaming: false,
    resuming: !!config.activeRunId,
    _source: null,
    _activeRun: null,
    _replayDedup: null,
    _toolIndex: new Map(),
    _reasoningIndex: new Map(),
    _scrollQueued: false,
    _autoFollow: true,
    _thinkingTimer: null,
    _scrollListener: null,
    _thinkingPhrase: THINKING_LABELS[0],
    // Wall-clock origin of the run in progress, 0 when idle. Read by the loader's
    // `elapsed` clock; set where a run actually starts, never inferred.
    runStartedAt: 0,
    filesTouchedLimit: 20,
    // Reactive clock backing relative timestamps: a single interval bumps it
    // (init/destroy) so every `relativeTime()` label recomputes instead of freezing.
    now: 0,
    _nowTimer: null,
    _announceOpen: null,
    // Which composer sheet is open: "" | "options" | "progress". One at a time — the two
    // are separate surfaces on purpose (what you configure vs. what the run produced).
    sheet: "",
    // Translated fragments the JS composes counts out of; the server owns the wording.
    _labels: { ...DEFAULT_LABELS, ...(config.labels || {}) },
    // Tools group. The catalog is every server this user could load; the selection is a
    // subset of the *available* ones. A never-touched thread carries no stored selection,
    // which means "all of them" — and keeps meaning that as servers are added later.
    mcpCatalog: loadMcpCatalog(),
    mcpSelected: [],
    mcpExpanded: false,
    mcpQuery: "",
    // Frozen row order for as long as the list stays open, so a row the user just toggled
    // never slides out from under their finger. Recomputed on each expand.
    mcpOrder: [],
    // Whether the user changed the selection in this page session. Only then does submit()
    // send one — otherwise an untouched thread keeps its NULL and picks up new servers.
    _mcpTouched: false,
    // "/" autocomplete: slash commands + global skills, server-rendered into the page.
    slashCatalog: loadSlashCatalog(),
    slashDismissed: false,
    slashIndex: 0,
    _announceSlashOpen: null,

    // The new-chat repo picker is its own Alpine root; it dispatches the
    // `daiv:chat-repo-changed` window event whenever its single-repo selection
    // changes. The chat root listens declaratively (see chat_detail.html) and
    // calls `applyRepoSelection()` so the proxy assignment goes through Alpine's
    // reactivity (an `addEventListener` from inside `init()` does not).
    applyRepoSelection(repos) {
      const first = (repos || [])[0];
      this.draftRepoId = first?.repo_id || "";
      this.draftRef = first?.ref || "";
      if (this.draftRepoId) {
        this.$nextTick(() => this.$refs.prompt?.focus());
      }
    },

    applyAgentSelection(detail) {
      // The agent picker is the single source of truth for the pill label and the spec
      // submit() forwards — we just stamp whatever it dispatched.
      this._agentModel = detail?.model || "";
      this._agentThinkingLevel = detail?.thinking_level || "";
      this.lockedAgentLabel = detail?.label || "";
    },

    // Effort word after the model name in the composer's label. Derived rather than a
    // second stored copy of ``_agentThinkingLevel``, which drifts the moment one of the
    // two assignments is missed; the seed covers the render before the picker dispatches.
    get lockedAgentThinking() {
      return this._agentThinkingLevel || config.initialAgentThinking || "";
    },

    // ---------- Composer sheets ---------------------------------------

    openSheet(name) {
      this.sheet = name;
      this._announceOpen();
    },

    closeSheet() {
      this.sheet = "";
    },

    toggleSheet(name) {
      if (this.sheet === name) this.closeSheet();
      else this.openSheet(name);
    },

    // ---------- Tools (MCP servers) -----------------------------------

    // One ordered list for every row the sheet draws — switchable first, then enabled,
    // then by name. Order is computed once per expand and held in ``mcpOrder`` so toggling
    // never reshuffles the list; search filters that order rather than replacing it.
    // Unavailable servers sort last and render inert, but stay listed: dropping them here
    // would drop them from the next selection this page posts, and keep them out of the
    // thread long after the outage was fixed.
    get mcpVisible() {
      const byName = new Map(this.mcpCatalog.map((s) => [s.name, s]));
      const ordered = this.mcpOrder.map((n) => byName.get(n)).filter(Boolean);
      const q = this.mcpQuery.trim().toLowerCase();
      return q ? ordered.filter((s) => s.name.toLowerCase().includes(q)) : ordered;
    },

    // Drives the badge dot on the options trigger. The env can't change mid-thread, so a
    // selection that deviates from the pool defaults is the only thing the dot can ever be
    // reporting — the same deviation the server stores as ``Session.mcp_overrides``.
    get mcpDirty() {
      const defaults = this._defaultMcpNames();
      return this.mcpSelected.length !== defaults.length
        || defaults.some((name) => !this.isMcpOn(name));
    },

    _defaultMcpNames() {
      return this.mcpCatalog.filter((s) => s.is_default).map((s) => s.name);
    },

    isMcpOn(name) {
      return this.mcpSelected.includes(name);
    },

    toggleMcp(name) {
      // A broken server (undecryptable headers, a missing env-var reference) loads zero
      // tools whatever the switch says, so its row is inert rather than silently useless.
      if (!this.mcpCatalog.some((s) => s.name === name && s.available)) return;
      this.mcpSelected = this.isMcpOn(name)
        ? this.mcpSelected.filter((n) => n !== name)
        : [...this.mcpSelected, name];
      this._mcpTouched = true;
    },

    selectNoMcp() {
      this.mcpSelected = [];
      this._mcpTouched = true;
    },

    // Back to the pool defaults — the ``active`` servers, not every server in the list.
    // An ``on-demand`` server is loadable but opt-in, so reset turns it back off; the
    // server stores the resulting empty diff, which keeps tracking the admin's defaults.
    resetMcp() {
      this.mcpSelected = this._defaultMcpNames();
      this._mcpTouched = true;
    },

    toggleMcpList() {
      this.mcpExpanded = !this.mcpExpanded;
      if (this.mcpExpanded) this._freezeMcpOrder();
    },

    _freezeMcpOrder() {
      this.mcpOrder = [...this.mcpCatalog]
        .sort((a, b) => {
          const availDelta = Number(b.available) - Number(a.available);
          const onDelta = Number(this.isMcpOn(b.name)) - Number(this.isMcpOn(a.name));
          return availDelta || onDelta || a.name.localeCompare(b.name);
        })
        .map((s) => s.name);
    },

    // "31 tools · filtered to 6" — the effect of MCPServer.tool_filter_mode, not a
    // second copy of the control.
    mcpToolsLabel(server) {
      if (!server.synced) return this._labels.notSynced;
      const base = `${server.tools} ${server.tools === 1 ? this._labels.tool : this._labels.tools}`;
      return server.filtered ? `${base} · ${this._labels.filteredTo} ${server.exposed}` : base;
    },

    // ---------- "/" autocomplete --------------------------------------

    get slashToken() {
      const m = SLASH_TOKEN_RE.exec(this.draftMessage);
      return m ? m[1].toLowerCase() : null;
    },

    get slashMatches() {
      const token = this.slashToken;
      if (token === null) return [];
      if (!token) return this.slashCatalog;
      const starts = [];
      const contains = [];
      for (const row of this.slashCatalog) {
        const name = row.name.toLowerCase();
        if (name.startsWith(token)) starts.push(row);
        else if (name.includes(token)) contains.push(row);
      }
      return [...starts, ...contains];
    },

    // A token that matches nothing hides the menu entirely: unlike the MCP filter's
    // empty row inside a sheet the user opened, this menu is unsolicited.
    get slashMenuOpen() {
      return this.slashToken !== null
        && !this.slashDismissed
        && !(this.streaming || this.resuming)
        && this.slashMatches.length > 0;
    },

    onPromptInput() {
      this.slashDismissed = false;
      this.slashIndex = 0;
    },

    // One handler instead of per-key `@keydown.*.prevent` bindings: Alpine's modifiers
    // preventDefault unconditionally, and plain Enter must keep inserting newlines
    // whenever the menu is closed.
    onPromptKeydown(e) {
      if (!this.slashMenuOpen) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        this.moveSlashHighlight(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        this.moveSlashHighlight(-1);
      } else if ((e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.shiftKey) || (e.key === "Tab" && !e.shiftKey)) {
        e.preventDefault();
        this.selectHighlightedSlash();
      } else if (e.key === "Escape") {
        // The topmost surface consumes Escape — don't let the dock's window-level
        // closeSheet() listener also fire.
        e.stopPropagation();
        this.slashDismissed = true;
      }
    },

    moveSlashHighlight(delta) {
      const count = this.slashMatches.length;
      if (!count) return;
      this.slashIndex = (this.slashIndex + delta + count) % count;
      this.$nextTick(() => {
        this.$refs.slashList?.querySelector(".env-popover__row--highlighted")
          ?.scrollIntoView({ block: "nearest" });
      });
    },

    selectHighlightedSlash() {
      const row = this.slashMatches[this.slashIndex];
      if (row) this.selectSlashCommand(row);
    },

    // Whole-draft replacement is safe: the menu is only open while the entire draft is
    // the token. The trailing space breaks SLASH_TOKEN_RE, which closes the menu.
    selectSlashCommand(row) {
      this.draftMessage = `/${row.name} `;
      this.$nextTick(() => {
        this.autosize();
        const el = this.$refs.prompt;
        if (el) {
          el.focus();
          el.setSelectionRange(el.value.length, el.value.length);
        }
      });
    },

    applyPolledTurns(turns) {
      // Background (non-chat) runs — schedules, webhooks, UI jobs — execute
      // detached and never publish to the chat relay, so there is no live stream
      // to join. The detail page polls the turns endpoint instead and re-emits
      // the WHOLE transcript here every few seconds. Reassigning ``turns`` re-runs
      // every x-html/x-text binding (re-rendered markdown, re-highlighted code,
      // repaint) even when nothing changed — the visible every-poll flicker. Skip
      // the swap when the transcript is byte-identical to what we already show, so
      // steady-state polls during a long-running tool are free; only a genuine
      // change repaints. Both sides come from the same server serializer
      // (annotate_transcript(build_turns(...))), so key order is stable and the
      // JSON compare is reliable. Alpine's keyed x-for reuses each turn's DOM node
      // on reassignment, so open tool <details> survive a real change too.
      if (JSON.stringify(turns) === JSON.stringify(this.turns)) return;
      this.turns = turns;
      // Mirror the live-stream path (dispatch() scrolls after every event): follow
      // the tail once the new turns render. The no-force scrollToBottom() no-ops
      // when the user has scrolled up to read, so it only auto-follows at bottom.
      this.$nextTick(() => this.scrollToBottom());
    },

    applySandboxEnvSelection(detail) {
      this.selectedSandboxEnvId = detail?.id || "";
      // ``daiv:env-changed`` payload is {id, name, scope}; empty id = Auto pick. An id
      // with empty name means the picker couldn't resolve the id against its envs list
      // (env removed mid-session, or the picker was mounted with a stale id) — surface
      // it via warn so the staleness is debuggable rather than blanking the pill, and
      // visually treat it as Auto so the locked label never renders empty.
      if (!detail?.id) {
        this.lockedEnvLabel = this._envAutoLabel;
        this.lockedEnvScope = "";
      } else if (!detail?.name) {
        console.warn("daiv:env-changed: id %o has no matching env; falling back to Auto label", detail.id);
        this.lockedEnvLabel = this._envAutoLabel;
        this.lockedEnvScope = "";
      } else {
        this.lockedEnvLabel = detail.name;
        this.lockedEnvScope = detail.scope || "";
      }
    },

    init() {
      this._announceOpen = surfaceGroup.join(() => this.closeSheet());

      // Its own surfaceGroup slot: an opening sheet dismisses the menu and vice versa.
      // $watch fires on transitions only, giving the closed→open-only announce.
      this._announceSlashOpen = surfaceGroup.join(() => { this.slashDismissed = true; });
      this.$watch("slashMenuOpen", (open) => {
        if (open) this._announceSlashOpen();
      });

      // Seed + 60s ticker (the `now` field explains why reassignment re-renders labels).
      this.now = Date.now();
      this._nowTimer = setInterval(() => { this.now = Date.now(); }, 60000);

      // The server already resolved the thread's stored overrides against the live pool,
      // so this is the effective selection, not a raw list to re-interpret. It is still
      // intersected with the catalog — and with the whole pool rather than just the
      // servers that are healthy right now, since a broken one dropped here would be
      // dropped from the next selection this page posts and would stay out of the thread
      // long after the outage was fixed.
      const stored = loadMcpSelection();
      this.mcpSelected = stored
        ? this.mcpCatalog.filter((s) => stored.includes(s.name)).map((s) => s.name)
        : this._defaultMcpNames();
      this._freezeMcpOrder();

      // A progress sheet whose trigger just disappeared (follow-up ask clears the todos
      // and files) would otherwise stay "open" and pop back into view on the next turn.
      this.$watch("!progressPill", (gone) => {
        if (gone && this.sheet === "progress") this.sheet = "";
      });

      // <main> is the actual scroll container — body is h-dvh, so the window
      // never scrolls; main holds overflow-y-auto and is the page surface.
      const scroller = document.querySelector("main");
      if (scroller) {
        const onScroll = () => {
          const distanceFromBottom =
            scroller.scrollHeight - (scroller.scrollTop + scroller.clientHeight);
          this._autoFollow = distanceFromBottom < 120;
        };
        scroller.addEventListener("scroll", onScroll, { passive: true });
        this._scrollListener = onScroll;
        this._scrollEl = scroller;
      }

      this.$watch("streaming", (on) => {
        if (on || this.resuming) {
          let i = 0;
          this._thinkingPhrase = THINKING_LABELS[0];
          this._thinkingTimer = setInterval(() => {
            i = (i + 1) % THINKING_LABELS.length;
            this._thinkingPhrase = THINKING_LABELS[i];
          }, 1800);
        } else {
          clearInterval(this._thinkingTimer);
          this._thinkingTimer = null;
          this.runStartedAt = 0;
        }
      });

      if (this.resuming && this.thread && config.activeRunId) {
        // Page loaded while a run is executing server-side: rejoin its event
        // stream with a full replay, deduping anything already rendered from
        // the checkpoint hydration. The clock counts from the server-reported
        // start so a rejoined run doesn't restart at 0.
        this.runStartedAt = Date.parse(config.activeRunStartedAt || "") || Date.now();
        this._resumeRun(config.activeRunId);
      } else {
        this.resuming = false;
      }

      // Park the viewport on page load. $nextTick waits for Alpine to
      // materialize x-for'd turns into DOM; parkViewport() then settles the
      // height shifts that land after that first render.
      if (this.turns.length) {
        this.$nextTick(() => this.parkViewport());
      }
    },

    destroy() {
      if (this._scrollListener && this._scrollEl) {
        this._scrollEl.removeEventListener("scroll", this._scrollListener);
      }
      if (this._thinkingTimer) clearInterval(this._thinkingTimer);
      if (this._nowTimer) clearInterval(this._nowTimer);
      if (this._source) this._source.close();
    },

    // ---------- Run stream (EventSource against the relay) -------------

    async _resumeRun(runId) {
      // LangChain message ids (turn.id) and tool_call ids from the hydrated
      // checkpoint are the same ids the AG-UI events carry — anything already
      // rendered server-side gets skipped during replay.
      const messages = new Set();
      const tools = new Set();
      for (const t of this.turns) {
        if (t.role === "run_status") continue;
        if (t.id) messages.add(t.id);
        for (const s of t.segments) {
          if (s.type === "tool_call" && s.id) tools.add(s.id);
        }
      }
      this._replayDedup = { messages, tools };

      this.turns.push({ id: uuid(), role: "assistant", segments: [], streaming: true });
      const turn = this.turns[this.turns.length - 1];
      this._toolIndex.clear();
      this._reasoningIndex.clear();
      this._activeRun = { threadId: this.thread.thread_id, runId };
      this.resuming = false;
      this.streaming = true;
      this.$nextTick(() => this.scrollToBottom({ force: true }));

      const reason = await this._streamRun(this._activeRun, turn);
      this._finishTurn(turn, reason);
    },

    _streamRun(run, turn) {
      const url =
        this.streamEndpoint +
        "?thread_id=" + encodeURIComponent(run.threadId) +
        "&run_id=" + encodeURIComponent(run.runId);
      return new Promise((resolve) => {
        const source = new EventSource(url);
        this._source = source;
        const finish = (reason) => {
          source.close();
          this._source = null;
          resolve(reason);
        };
        source.onmessage = (event) => {
          let evt;
          try {
            evt = JSON.parse(event.data);
          } catch (err) {
            console.error("chat: malformed SSE frame, skipping", err);
            return;
          }
          if (this._isReplayDuplicate(evt)) return;
          this.dispatch(evt, turn);
        };
        source.addEventListener("end", (event) => {
          let reason = "finished";
          try {
            reason = JSON.parse(event.data || "{}").reason || reason;
          } catch {
            /* keep default */
          }
          finish(reason);
        });
        source.onerror = () => {
          // EventSource auto-reconnects (re-sending Last-Event-ID) on transient
          // drops and on the server's duration-cap close; only a permanently
          // CLOSED source is fatal.
          if (source.readyState === EventSource.CLOSED) finish("connection_lost");
        };
      });
    },

    _isReplayDuplicate(evt) {
      const d = this._replayDedup;
      if (!d) return false;
      if (evt.messageId && d.messages.has(evt.messageId)) return true;
      if (evt.toolCallId && d.tools.has(evt.toolCallId)) return true;
      return false;
    },

    _finishTurn(turn, reason) {
      turn.streaming = false;
      turn.segments.forEach((s) => {
        if (s.type === "tool_call" && s.status === "running") s.status = "done";
        if (s.type === "thinking" && s.status === "running") {
          s.status = "done";
          s.endedAt = Date.now();
        }
      });
      // Terminal reasons that leave the turn in an error state ("finished" is
      // clean and absent here). "error" = the server hit a relay/backend fault
      // tailing the stream and sent an explicit error end frame rather than
      // dropping silently. Never clobber an in-band run_status already pushed.
      const REASON_ERRORS = {
        stale: "The run stopped responding — refresh to check its final state.",
        connection_lost: "Lost connection to the server — refresh to continue.",
        error: "The live stream failed — refresh to check the run's state.",
      };
      if (REASON_ERRORS[reason] && !this._hasRunStatusMarker()) {
        this._pushRunStatus("failed", REASON_ERRORS[reason]);
      }
      this.streaming = false;
      this._activeRun = null;
      this._replayDedup = null;
      this.scrollToBottom();
    },

    // ---------- Derived getters (right rail) ---------------------------

    get runStatus() {
      const last = this.turns[this.turns.length - 1];
      if (last?.role === "run_status") {
        return last.status === "aborted"
          ? { tone: "idle", label: "stopped" }
          : { tone: "error", label: "error" };
      }
      if (this.resuming && !this.streaming) {
        return { tone: "thinking", label: "catching up on the running session…" };
      }
      if (!this.streaming) return { tone: "idle", label: "idle" };
      // Publish-phase chips win over generic tool calls in the status bar:
      // when GitMiddleware is committing/creating an MR, that's the most
      // informative thing to surface.
      const activePhase = last?.segments.slice().reverse().find(
        (s) => s.type === "publish_phase" && s.status === "running",
      );
      if (activePhase) return { tone: "running", label: `${activePhase.label}…` };
      const activeTool = last?.segments.slice().reverse().find(
        (s) => s.type === "tool_call" && s.status === "running",
      );
      if (activeTool) return { tone: "running", label: `running ${activeTool.name}…` };
      return { tone: "thinking", label: this._thinkingPhrase };
    },

    get latestTodos() {
      // Cached for the rest of the microtask like ``filesTouched``, for the same reason:
      // the progress pill and sheet read this a dozen-plus times per Alpine flush, and
      // each read re-walked the transcript and re-parsed the todo args.
      if (todosCache) return todosCache;
      todosCache = findLatestTodos(this.turns);
      queueMicrotask(() => { todosCache = null; });
      return todosCache;
    },

    get todosDone() {
      return this.latestTodos.filter((t) => (t.status || "").toLowerCase() === "completed").length;
    },

    get filesTouched() {
      // Alpine re-evaluates every binding that reads this in a single flush — the pill,
      // its class, the crowded modifier, the sheet's rows, the $watch. Caching for the
      // rest of the current microtask collapses those into one transcript walk; releasing
      // it there means no mutation can ever be observed through a stale cache, since
      // `turns` only changes between events.
      if (filesCache) return filesCache;
      // path -> { path, op, fromPath?, segmentId }
      const map = new Map();
      const record = (path, op, seg, extra = {}) => {
        if (!path) return;
        map.set(path, { path, op, segmentId: `tool-${seg.id}`, ...extra });
      };
      for (const t of this.turns) {
        if (t.role === "run_status") continue;
        for (const seg of t.segments) {
          if (seg.type !== "tool_call") continue;
          if (PATH_TOOLS.has(seg.name)) {
            const op = seg.name === "write_file" ? "added" : "modified";
            record(pickPath(seg.args), op, seg);
          } else if (seg.name === "bash") {
            for (const entry of bashFilesChanged(seg.result)) {
              record(entry.path, entry.op || "modified", seg,
                entry.from_path ? { fromPath: entry.from_path } : {});
            }
          }
        }
      }
      // Reverse so most-recent comes first.
      filesCache = [...map.values()].reverse();
      queueMicrotask(() => { filesCache = null; });
      return filesCache;
    },

    get showJumpToLatest() {
      return this.streaming && !this._autoFollow;
    },

    // ---------- Progress trigger --------------------------------------

    // The composer's progress pill, or ``null`` when there is nothing behind it. The
    // decision is made on *content*, never on run state: no todos, no files and no diff
    // means no button, because an empty sheet is worse than no way into one.
    //
    // Mid-turn the pill reports counts only — the work isn't committed yet, so line
    // numbers would be a guess. A cancelled turn is the one state where work can sit
    // uncommitted for good, so it keeps counts too.
    get progressPill() {
      const todos = this.latestTodos;
      const files = this.filesTouched;
      const parts = [];
      if (todos.length) parts.push(`${this.todosDone}/${todos.length}`);
      if (files.length) parts.push(this._filesLabel(files.length));

      if (this.streaming || this.resuming) {
        return parts.length ? { tone: "live", label: parts.join(" · ") } : null;
      }
      // A cancelled turn can leave work uncommitted for good, so counts stay the only
      // truth there — a line total would describe something that was never published.
      if (this._lastRunAborted()) {
        return parts.length ? { tone: "warn", label: [this._labels.stopped, ...parts].join(" · ") } : null;
      }
      // ``diff`` rather than a joined label: added and removed are tinted separately,
      // the way they are everywhere else a diff is reported.
      const stats = this.diffStats;
      if (stats && (stats.added || stats.removed)) {
        return { tone: "idle", label: "", diff: stats };
      }
      return parts.length ? { tone: "idle", label: parts.join(" · ") } : null;
    },

    // What the hero's selection line shows before a thread exists: the two things the
    // first turn will run with that aren't already on screen.
    get heroSelectionLabel() {
      return `${this.lockedEnvLabel} · ${this.mcpSelected.length} ${this._labels.mcpServers}`;
    },

    _filesLabel(n) {
      return `${n} ${n === 1 ? this._labels.file : this._labels.files}`;
    },

    _lastRunAborted() {
      const last = this.turns[this.turns.length - 1];
      return last?.role === "run_status" && last.status === "aborted";
    },

    // ---------- Rendering helpers used inline by x-html ---------------

    renderMarkdown(raw) {
      return window.renderMarkdown ? window.renderMarkdown(raw) : "";
    },

    visibleSegments(turn) {
      return turn.segments.filter(
        (s) => !(s.type === "tool_call" && s.name === "write_todos"),
      );
    },

    isTurnVisible(turn, isLast) {
      if (turn.role === "run_status") return true;
      if (this.visibleSegments(turn).length) return true;
      // Keep an empty assistant turn only while it is streaming (thinking indicator).
      return turn.role === "assistant" && isLast && turn.streaming;
    },

    // ---------- Per-turn action row (copy + timestamps) ---------------

    // The copy payload and the gate for whether the copy button renders: the raw
    // source the bubble rendered (markdown on assistant turns, plain text on user
    // ones), or null for a tool-only turn.
    finalTextSegment(turn) {
      for (let i = turn.segments.length - 1; i >= 0; i--) {
        if (turn.segments[i].type === "text") return turn.segments[i];
      }
      return null;
    },

    // Only a run's closing text turn is copyable; earlier ones are narration. Runs are serial
    // and run_status markers terminal, so the next non-assistant turn is always the boundary.
    isClosingTextTurn(ti) {
      for (let i = ti + 1; i < this.turns.length; i++) {
        const later = this.turns[i];
        if (later.role !== "assistant") return true;
        if (later.streaming || this.finalTextSegment(later)) return false;
      }
      return true;
    },

    // On a user turn this copies the whole message, including the part the
    // "Show more" clamp is hiding.
    canCopyTurn(turn, ti) {
      if (turn.role === "run_status" || turn.streaming || !this.finalTextSegment(turn)) return false;
      return turn.role === "user" || this.isClosingTextTurn(ti);
    },

    // Resolves true only once the write actually lands. The clipboard API is
    // absent in insecure contexts (plain-HTTP LAN) and writeText() can reject
    // (permission / unfocused doc) — the caller drives its confirmation off this
    // so the UI never claims a copy it didn't make.
    async copyFinalText(turn) {
      const seg = this.finalTextSegment(turn);
      if (!seg || !navigator.clipboard) return false;
      try {
        await navigator.clipboard.writeText(seg.content);
        return true;
      } catch (e) {
        console.warn("chat: clipboard write failed", e);
        return false;
      }
    },

    // Timestamps first: they short-circuit finalTextSegment's segment scan on historical turns.
    turnHasActions(turn, ti) {
      return turn.role !== "run_status" && (!!turn.sent_at || !!turn.received_at || this.canCopyTurn(turn, ti));
    },

    // Reads `this.now` so the ticker's bumps recompute it; absolute date past a month.
    // Untranslated, matching the existing client-side elapsed timer.
    relativeTime(iso) {
      if (!iso) return "";
      const then = new Date(iso).getTime();
      if (!Number.isFinite(then)) return "";
      const sec = Math.floor(Math.max(0, this.now - then) / 1000);
      if (sec < 60) return "just now";
      const min = Math.floor(sec / 60);
      if (min < 60) return `${min} minute${min === 1 ? "" : "s"} ago`;
      const hr = Math.floor(min / 60);
      if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
      const day = Math.floor(hr / 24);
      if (day < 30) return `${day} day${day === 1 ? "" : "s"} ago`;
      return new Date(iso).toLocaleDateString();
    },

    absoluteTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      return Number.isFinite(d.getTime()) ? d.toLocaleString() : "";
    },

    toolSignature(seg) {
      if (!window.toolSignature) return { label: seg.name, path: "", badges: [] };
      return window.toolSignature(seg.name, seg.args, seg.result, seg.status);
    },

    toolBodyHTML(seg) {
      if (!window.toolBodyHTML) return "";
      return window.toolBodyHTML(seg.name, seg.args, seg.result, seg.status);
    },

    thinkingLabel(seg) {
      if (seg.status === "running") return "Thinking…";
      if (!seg.startedAt || !seg.endedAt) return "Reasoning";
      const s = Math.max(1, Math.round((seg.endedAt - seg.startedAt) / 1000));
      return `Thought for ${formatElapsed(s)}`;
    },

    // Status-letter marks, as the progress sheet's rows are specified. ``+``/``−``/``~``
    // would read as line counts in a list that also carries them.
    fileOpMark(op) {
      switch ((op || "modified").toLowerCase()) {
        case "added": return "A";
        case "deleted": return "D";
        case "renamed": return "R";
        default: return "M";
      }
    },

    todoIcon(status) {
      const s = (status || "pending").toLowerCase();
      if (s === "completed") return "✓";
      if (s === "in_progress") return "▸";
      return "·";
    },

    // ---------- User actions ------------------------------------------

    canSend() {
      if (!this.draftMessage.trim()) return false;
      if (this.thread) return true;
      return !!(this.draftRepoId && this.draftRef);
    },

    autosize() {
      const el = this.$refs.prompt;
      if (!el || NATIVE_FIELD_SIZING) return;
      el.style.height = "auto";
      el.style.height = el.scrollHeight + "px";
    },

    async submit() {
      if (!this.canSend() || this.streaming || this.resuming) return;

      // Read the picker's selection before creating the thread: the live picker is
      // ``x-if``'d on ``!thread``, so assigning ``thread`` first schedules its removal
      // and the hidden-input fallback below would race Alpine's DOM flush.
      // ``_agentModel`` is normally already set (the picker dispatches on init); the
      // inputs cover a submit that beats that first dispatch.
      const agentModel = this._agentModel
        || this.$root?.querySelector?.('input[name="agent_model"]')?.value
        || "";
      const agentThinkingLevel = this._agentThinkingLevel
        || this.$root?.querySelector?.('input[name="agent_thinking_level"]')?.value
        || "";

      if (!this.thread) {
        const threadId = uuid();
        this.thread = { thread_id: threadId, repo_id: this.draftRepoId, ref: this.draftRef };
        history.replaceState(null, "", `/dashboard/chat/${threadId}/`);
      }

      this.turns.push({
        id: uuid(),
        role: "user",
        segments: [{ type: "text", content: this.draftMessage }],
        // Optimistic stamp; a reload reconciles it to the server's Run.created_at and
        // relative granularity hides the difference. `received_at` is server-owned.
        sent_at: new Date().toISOString(),
      });
      this.turns.push({
        id: uuid(),
        role: "assistant",
        segments: [],
        streaming: true,
      });
      const assistantTurn = this.turns[this.turns.length - 1];
      this._toolIndex.clear();
      this._reasoningIndex.clear();
      this._autoFollow = true;
      this.$nextTick(() => this.scrollToBottom({ force: true }));

      // Send only user turns. Streamed assistant turns carry a client-minted
      // UUID (set when we pushed the placeholder), but the server stored the
      // AIMessage under its own LangChain-generated id — echoing the assistant
      // turn back would slip past ag_ui_langgraph's id-based dedupe and append
      // a duplicate AIMessage to the checkpoint. The agent reads prior history
      // from the checkpointer; it doesn't need the client to replay it.
      const priorMessages = this.turns
        .slice(0, -1)
        .filter((t) => t.role === "user")
        .map((t) => ({
          id: t.id,
          role: t.role,
          content: t.segments
            .filter((s) => s.type === "text")
            .map((s) => s.content)
            .join("\n\n"),
        }))
        .filter((m) => m.content);

      // The server pins these to ``Session.agent_model`` / ``agent_thinking_level`` on
      // first sight of the thread and rejects a divergent value afterwards.
      const forwardedProps = {};
      if (agentModel) forwardedProps.agent_model = agentModel;
      if (agentThinkingLevel) forwardedProps.agent_thinking_level = agentThinkingLevel;
      // Only sent once the user has actually touched the Tools sheet: an absent key tells
      // the server to keep the thread's stored overrides. Sending an untouched selection
      // would be harmless but would re-diff it against a pool the admin may have changed.
      if (this._mcpTouched) forwardedProps.mcp_servers = [...this.mcpSelected];

      const body = {
        threadId: this.thread.thread_id,
        runId: uuid(),
        state: {},
        messages: priorMessages,
        tools: [],
        context: [],
        forwardedProps,
      };

      this.draftMessage = "";
      this.$nextTick(() => this.autosize());
      this.streaming = true;
      this.runStartedAt = Date.now();
      this._activeRun = { threadId: this.thread.thread_id, runId: body.runId };

      // Forward the picker's selection so the API resolves the requested env per-request;
      // missing or empty falls through to the GLOBAL default on the server.
      const envHeaders = this.selectedSandboxEnvId ? { "X-Sandbox-Env": this.selectedSandboxEnvId } : {};

      let reason = "finished";
      try {
        const resp = await fetch(this.endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Repo-ID": this.thread.repo_id,
            "X-Ref": this.thread.ref,
            "X-CSRFToken": this.csrfToken,
            ...envHeaders,
          },
          body: JSON.stringify(body),
          credentials: "include",
        });

        if (!resp.ok) {
          this._pushRunStatus("failed", await formatHttpError(resp));
          return;
        }

        // The run now executes server-side detached from any connection; all
        // event consumption goes through the resumable relay stream.
        reason = await this._streamRun(this._activeRun, assistantTurn);
      } catch (err) {
        console.error("chat: failed to start run", err);
        this._pushRunStatus("failed", "Connection lost — please retry.");
      } finally {
        this._finishTurn(assistantTurn, reason);
      }
    },

    async stop() {
      // Disconnects no longer stop the run — cancellation is explicit. The
      // stream stays open so the server's RUN_ERROR(run_cancelled) event and
      // end frame settle the turn state.
      if (!this._activeRun || !this.cancelEndpoint) return;
      try {
        const resp = await fetch(this.cancelEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": this.csrfToken },
          body: JSON.stringify({
            thread_id: this._activeRun.threadId,
            run_id: this._activeRun.runId,
          }),
          credentials: "include",
        });
        // 409 = the run already left the in-flight slot (finishing/finished);
        // the still-open stream will settle the turn, so that's benign. Any
        // other non-OK status means the stop didn't take — warn rather than let
        // the button silently appear to do nothing.
        if (!resp.ok && resp.status !== 409) {
          console.warn("chat: cancel rejected with status", resp.status);
        }
      } catch (err) {
        console.warn("chat: cancel request failed", err);
      }
    },

    jumpToTool(segmentId) {
      const el = document.getElementById(segmentId);
      if (!el) return;
      if (el.tagName.toLowerCase() === "details") el.open = true;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("chat-tool__highlight");
      void el.offsetWidth; // restart animation
      el.classList.add("chat-tool__highlight");
    },

    dispatch(evt, turn) {
      const type = evt.type;

      if (type === AGUI.TEXT_MESSAGE_START) {
        this._appendTextSegment(turn, "");
      } else if (type === AGUI.TEXT_MESSAGE_CONTENT || type === AGUI.TEXT_MESSAGE_CHUNK) {
        const delta = evt.delta || evt.content || "";
        const last = turn.segments[turn.segments.length - 1];
        if (!last || last.type !== "text") {
          this._appendTextSegment(turn, delta);
        } else {
          last.content += delta;
        }
      } else if (type === AGUI.REASONING_START) {
        turn.segments.push({
          type: "thinking",
          id: evt.messageId,
          content: "",
          startedAt: Date.now(),
          endedAt: null,
          status: "running",
        });
        this._reasoningIndex.set(evt.messageId, turn.segments.length - 1);
      } else if (type === AGUI.REASONING_MESSAGE_CONTENT) {
        const idx = this._reasoningIndex.get(evt.messageId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (seg) seg.content += evt.delta || "";
      } else if (type === AGUI.REASONING_END) {
        const idx = this._reasoningIndex.get(evt.messageId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (seg) {
          seg.status = "done";
          seg.endedAt = Date.now();
        }
      } else if (type === AGUI.TOOL_CALL_START) {
        // ag_ui_langgraph re-emits START/ARGS/END from OnToolEnd whenever its
        // `has_function_streaming` flag was reset to False by an inner tool
        // completion. Dedupe by tool_call_id and seal the existing segment so
        // the follow-up ARGS delta does not double-serialize the input. The
        // server-side filter already drops the late re-emit for `task` tools
        // (see ``chat.api.views._filter_subagent_events``); this guard keeps
        // any other tool that hits the same upstream path safe.
        const existingIdx = this._toolIndex.get(evt.toolCallId);
        if (existingIdx != null && turn.segments[existingIdx]) {
          turn.segments[existingIdx].sealed = true;
          return;
        }
        const phase = PUBLISH_PHASE_TOOLS[evt.toolCallName];
        if (phase) {
          // Structured-response tool from the publish pipeline: drop the args/
          // result rendering entirely and surface a phase chip. The chip stays
          // on the running turn until TOOL_CALL_RESULT (or RUN_FINISHED) flips
          // it to done.
          turn.segments.push({
            type: "publish_phase",
            id: evt.toolCallId,
            name: evt.toolCallName,
            label: phase.label,
            status: "running",
          });
        } else {
          turn.segments.push({
            type: "tool_call",
            id: evt.toolCallId,
            name: evt.toolCallName,
            args: "",
            result: null,
            status: "running",
          });
        }
        this._toolIndex.set(evt.toolCallId, turn.segments.length - 1);
      } else if (type === AGUI.TOOL_CALL_ARGS) {
        const idx = this._toolIndex.get(evt.toolCallId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (!seg) {
          console.warn("chat: TOOL_CALL_ARGS for unknown tool_call_id", evt.toolCallId);
        } else if (seg.type === "tool_call" && !seg.sealed) {
          // Phase chips intentionally ignore args (the structured-response JSON
          // is not user-facing).
          seg.args += evt.delta || "";
        }
      } else if (type === AGUI.TOOL_CALL_END) {
        // Structured-response tool calls don't always trigger a TOOL_CALL_RESULT
        // (the agent extracts the structured payload and stops without
        // executing a tool), so use the END signal — which always fires once
        // args streaming finishes — to flip the phase chip to done.
        const idx = this._toolIndex.get(evt.toolCallId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (!seg) {
          console.warn("chat: TOOL_CALL_END for unknown tool_call_id", evt.toolCallId);
        } else if (seg.type === "publish_phase") {
          seg.status = "done";
        }
      } else if (type === AGUI.TOOL_CALL_RESULT) {
        const idx = this._toolIndex.get(evt.toolCallId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (!seg) {
          console.warn("chat: TOOL_CALL_RESULT for unknown tool_call_id", evt.toolCallId);
          return;
        }
        if (seg.type === "publish_phase") {
          seg.status = "done";
        } else {
          seg.result = evt.content;
          seg.status = "done";
        }
      } else if (type === AGUI.RUN_ERROR) {
        turn.segments.forEach((s) => {
          if ((s.type === "tool_call" || s.type === "publish_phase") && s.status === "running") {
            s.status = "error";
          }
        });
        // Cancel and interrupt (stale takeover / shutdown) are neutral, non-failure
        // terminations → the grey "aborted" marker. Messages mirror the server constants
        // (core.constants) so the live marker matches what reload renders.
        if (evt.code === "run_cancelled") {
          this._pushRunStatus("aborted", "Stopped by user.");
        } else if (evt.code === "run_interrupted") {
          this._pushRunStatus("aborted", evt.message || "Run was interrupted before completing.");
        } else {
          this._pushRunStatus("failed", evt.message || "Run failed.");
        }
      } else if (type === AGUI.CUSTOM && evt.name === "ref_fallback") {
        // The pinned branch was merged/deleted; the server fell back to the default branch
        // and re-pinned the session. Move the composer ref pill so the UI matches reality.
        const v = evt.value || {};
        if (v.using) this._applyRepoState({ ref: v.using });
        console.debug("chat: ref_fallback %o → %o", v.requested, v.using);
      } else if (type === AGUI.CUSTOM && evt.name === "resolved_env") {
        // Server resolved Auto → real env for this run. Swap the locked pill text in
        // place when the user is still on Auto client-side; an explicit mid-flight
        // pick wins and we log the drop so client/server divergence stays debuggable.
        const v = evt.value || {};
        if (!this.selectedSandboxEnvId && v.id) {
          this.applySandboxEnvSelection({id: v.id, name: v.name || "", scope: v.scope || ""});
        } else if (this.selectedSandboxEnvId) {
          console.debug("chat: ignored resolved_env (user picked %o)", this.selectedSandboxEnvId, v);
        }
      } else if (type === AGUI.STATE_SNAPSHOT) {
        // Snapshots fire on every node exit and almost always carry an
        // unchanged merge_request. Dedupe on identity so we don't churn
        // Alpine reactivity (and the publish-phase chip sweep) per node.
        const snap = evt.snapshot || {};
        if ("diff_stats" in snap) {
          const stats = normalizeDiffStats(snap.diff_stats);
          if (stats && (this.diffStats?.added !== stats.added || this.diffStats?.removed !== stats.removed)) {
            this.diffStats = stats;
          }
        }
        if ("merge_request" in snap) {
          const raw = snap.merge_request;
          const key = raw ? `${raw.merge_request_id}:${raw.source_branch}:${raw.draft}` : "null";
          if (this._lastMrKey !== key) {
            this._lastMrKey = key;
            const mr = normalizeStateMr(raw);
            this._applyRepoState({ merge_request: mr, ref: mr ? mr.source_branch : undefined });
          }
        }
      } else {
        // Cheap visibility for unrecognised AG-UI events so future upstream
        // additions don't vanish silently from the chat UI.
        console.debug("chat: unhandled AG-UI event", type);
      }
      this.scrollToBottom();
    },

    _applyRepoState(value) {
      // Replace `thread` wholesale so Alpine reactivity propagates the new
      // `ref` and `merge_request` to the composer pills. `merge_request` is
      // applied only when the caller explicitly provided the key — an absent
      // key preserves the current pill, an explicit null clears it.
      if (this.thread) {
        const next = { ...this.thread, ref: value.ref || this.thread.ref };
        if ("merge_request" in value) next.merge_request = value.merge_request;
        this.thread = next;
      }
      // Belt-and-braces: once a snapshot carrying the published MR lands, the
      // publish pipeline is done. Any phase chip still marked running is stale
      // (likely a missed TOOL_CALL_END for a fast-finishing structured tool).
      if (value.merge_request) {
        for (const t of this.turns) {
          if (t.role === "run_status") continue;
          for (const s of t.segments) {
            if (s.type === "publish_phase" && s.status === "running") s.status = "done";
          }
        }
      }
    },

    _pushRunStatus(status, message) {
      const runId = this._activeRun ? this._activeRun.runId : uuid();
      const id = `run-status-${runId}`;
      const existing = this.turns.find((t) => t.id === id);
      if (existing) {
        existing.status = status;
        existing.message = message;
      } else {
        this.turns.push({ id, role: "run_status", status, message });
      }
      this.scrollToBottom();
    },

    _hasRunStatusMarker() {
      const runId = this._activeRun ? this._activeRun.runId : null;
      return runId != null && this.turns.some((t) => t.id === `run-status-${runId}`);
    },

    _appendTextSegment(turn, content) {
      turn.segments.push({ type: "text", content });
      return turn.segments[turn.segments.length - 1];
    },

    // Where the viewport lands on page load. A live session belongs at the
    // bottom, so streamed output arrives in view. A finished one opens at the
    // *top of the last assistant message* instead: that final answer is what
    // the reader came for, and anchoring to the bottom buries its opening
    // lines above the fold whenever it is taller than the viewport.
    parkViewport() {
      if (config.sessionLive) {
        this.scrollToBottom({ force: true });
        return;
      }
      // Two frames deep, because anything that changes height *above* the
      // target after we measure drags it back off the top edge. One rAF clears
      // the userClamp children, which queue their collapse on $nextTick; their
      // "Show more" toggles then reveal a frame later, growing each collapsed
      // bubble again. Chrome and Firefox would absorb that second shift with
      // scroll anchoring, but Safari has none — so settle it here instead of
      // depending on the browser to paper over it.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const el = this._scrollEl;
        if (!el) return;
        // Last rendered assistant turn — querying the DOM rather than `turns`
        // keeps this in step with isTurnVisible(), which drops empty turns.
        // `data-role` is the handle, not the `chat-turn--${role}` class: that
        // one is composed at render time and no stylesheet declares the
        // assistant variant, so keying on it would break silently.
        const rendered = el.querySelectorAll('.chat-turn[data-role="assistant"]');
        const target = rendered[rendered.length - 1];
        if (!target) {
          // A run that failed before answering: the tail is still the most
          // useful place to be.
          el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
          return;
        }
        // Breathing room above it comes from .chat-turn's scroll-margin-top,
        // and the browser clamps at the end of the scroll range — so a short
        // final answer degrades to the old bottom-anchored position for free.
        target.scrollIntoView({ block: "start", behavior: "auto" });
      }));
    },

    scrollToBottom({ force = false } = {}) {
      if (!force && !this._autoFollow) return;
      if (this._scrollQueued) return;
      this._scrollQueued = true;
      requestAnimationFrame(() => {
        this._scrollQueued = false;
        const el = this._scrollEl;
        if (!el) return;
        el.scrollTo({
          top: el.scrollHeight,
          behavior: force ? "smooth" : "auto",
        });
        if (force) this._autoFollow = true;
      });
    },
  };
  };

  // Collapse tall user-message bubbles. Measures the rendered height once on
  // init: user text is immutable after creation, and Alpine's keyed x-for reuses
  // the DOM node across the wholesale `turns` reassignment that polling performs,
  // so a single measurement is stable. Both inputs to the overflow test are read
  // from CSS so the JS decision can't drift from the visible clamp: the line
  // budget from the --chat-user-clamp-lines custom property, and the per-line
  // height from the element's computed line-height (CSS clamps to the same
  // budget * line-height, see .chat-text--clamped). `id` is the shared id the
  // text region exposes and the toggle points its aria-controls at.
  const userClamp = (id) => ({
    id,
    collapsed: false,
    overflowing: false,
    init() {
      this.$nextTick(() => {
        const el = this.$el.querySelector(".chat-text");
        if (!el) return;
        const styles = getComputedStyle(el);
        // Guard against a falsy 0 slipping through `||`: a 0-line budget is
        // nonsensical but would read back as the fallback 7 while the CSS clamp
        // honours 0, diverging the two. Number.isFinite rejects NaN (unreadable
        // property) too.
        const parsedLines = parseInt(styles.getPropertyValue("--chat-user-clamp-lines"), 10);
        const maxLines = Number.isFinite(parsedLines) && parsedLines > 0 ? parsedLines : 7;
        const lineHeight = parseFloat(styles.lineHeight) || 26;
        // +1 absorbs sub-pixel rounding so a bubble exactly maxLines tall is
        // left alone rather than clamped.
        if (el.scrollHeight > lineHeight * maxLines + 1) {
          this.overflowing = true;
          this.collapsed = true;
        }
      });
    },
  });

  // Ticking "how long has this run been going" clock, shared by the working loader
  // and the queued/running card. `track()` takes an ISO string or epoch ms and 0 to
  // stop; each tick re-derives from the origin rather than incrementing, so interval
  // clamping in a background tab costs accuracy nothing. Origin and handle stay
  // closure-local: only `seconds` needs to be reactive.
  const elapsed = (startedAt = 0) => {
    let origin = 0;
    let timer = null;
    return {
      seconds: 0,
      init() {
        this.track(startedAt);
      },
      get label() {
        return formatElapsed(this.seconds);
      },
      track(next) {
        const parsed = typeof next === "string" ? Date.parse(next) : next;
        const at = Number.isFinite(parsed) ? parsed : 0;
        if (at === origin) return;
        origin = at;
        clearInterval(timer);
        timer = null;
        this.seconds = 0;
        if (!at) return;
        const tick = () => {
          this.seconds = Math.max(0, Math.floor((Date.now() - origin) / 1000));
        };
        tick();
        timer = setInterval(tick, 1000);
      },
      destroy() {
        clearInterval(timer);
      },
    };
  };

  document.addEventListener("alpine:init", () => {
    window.Alpine.data("chat", chat);
    window.Alpine.data("userClamp", userClamp);
    window.Alpine.data("elapsed", elapsed);
  });
})();
