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

  const loadInitialMergeRequest = () => {
    const el = document.getElementById("chat-initial-merge-request");
    if (!el) return null;
    try {
      const v = JSON.parse(el.textContent);
      return v && typeof v === "object" ? v : null;
    } catch {
      return null;
    }
  };

  // Tools whose args directly name a file the agent *modified*. Read-only tools
  // (read_file, grep, glob, ls) and search patterns don't qualify — they're
  // noise in the "files touched" rail. Bash-driven mutations (rm, mv, scripts,
  // find -delete, …) are folded in from the bash tool's `files_changed` result.
  const PATH_TOOLS = new Set(["write_file", "edit_file"]);

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

  const loadInitialTurns = () => {
    const el = document.getElementById("chat-initial-turns");
    if (!el) return [];
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      console.error("chat: failed to parse server-embedded turns", err);
      return [];
    }
  };

  const THINKING_LABELS = [
    "Thinking…",
    "Reading files…",
    "Exploring the codebase…",
    "Understanding context…",
    "Planning the change…",
    "Running tools…",
  ];

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

  const chat = (config) => ({
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
    lockedAgentEffortDots: config.initialAgentEffortDots || 0,
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
    _filesSeen: new Set(),
    _scrollQueued: false,
    _autoFollow: true,
    _thinkingTimer: null,
    _scrollListener: null,
    _thinkingPhrase: THINKING_LABELS[0],
    filesTouchedLimit: 20,

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
      // The agent picker is the single source of truth for the pill label, effort
      // dots, and the spec submit() forwards — we just stamp whatever it dispatched.
      this._agentModel = detail?.model || "";
      this._agentThinkingLevel = detail?.thinking_level || "";
      this.lockedAgentLabel = detail?.label || "";
      this.lockedAgentEffortDots = detail?.effort_dots || 0;
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
      // Seed _filesSeen with any paths already present in hydrated history so
      // the "new row pulse" animation does not fire on initial load.
      for (const t of this.turns) {
        for (const seg of t.segments) {
          if (seg.type !== "tool_call") continue;
          if (PATH_TOOLS.has(seg.name)) {
            const p = pickPath(seg.args);
            if (p) this._filesSeen.add(`${seg.name}::${p}`);
          } else if (seg.name === "bash") {
            for (const entry of bashFilesChanged(seg.result)) {
              if (entry.path) this._filesSeen.add(`bash::${entry.path}`);
            }
          }
        }
      }

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
        } else if (this._thinkingTimer) {
          clearInterval(this._thinkingTimer);
          this._thinkingTimer = null;
        }
      });

      if (this.resuming && this.thread && config.activeRunId) {
        // Page loaded while a run is executing server-side: rejoin its event
        // stream with a full replay, deduping anything already rendered from
        // the checkpoint hydration.
        this._resumeRun(config.activeRunId);
      } else {
        this.resuming = false;
      }

      // Park the viewport at the latest turn on page load. $nextTick waits for
      // Alpine to materialize x-for'd turns into DOM so scrollHeight is final.
      if (this.turns.length) {
        this.$nextTick(() => this.scrollToBottom({ force: true }));
      }
    },

    destroy() {
      if (this._scrollListener && this._scrollEl) {
        this._scrollEl.removeEventListener("scroll", this._scrollListener);
      }
      if (this._thinkingTimer) clearInterval(this._thinkingTimer);
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
      if (reason === "stale" && !turn.error && !turn.aborted) {
        turn.error = "The run stopped responding — refresh to check its final state.";
      }
      if (reason === "connection_lost" && !turn.error && !turn.aborted) {
        turn.error = "Lost connection to the server — refresh to continue.";
      }
      this.streaming = false;
      this._activeRun = null;
      this._replayDedup = null;
      this.scrollToBottom();
    },

    // ---------- Derived getters (right rail) ---------------------------

    get runStatus() {
      const last = this.turns[this.turns.length - 1];
      if (last?.error) return { tone: "error", label: "error" };
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
      // Only consider write_todos calls from the current ask. Walking backwards
      // and bailing at the most recent user turn clears the rail on follow-up,
      // so stale "all complete" lists from a finished run don't linger.
      for (let i = this.turns.length - 1; i >= 0; i--) {
        const turn = this.turns[i];
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
    },

    get todosDone() {
      return this.latestTodos.filter((t) => (t.status || "").toLowerCase() === "completed").length;
    },

    get filesTouched() {
      // path -> { path, op, fromPath?, segmentId, isNew }
      const map = new Map();
      const seenKeys = [];
      const record = (path, op, seg, extra = {}) => {
        if (!path) return;
        const key = `${seg.name}::${path}`;
        seenKeys.push(key);
        map.set(path, {
          path,
          op,
          segmentId: `tool-${seg.id}`,
          isNew: !this._filesSeen.has(key),
          ...extra,
        });
      };
      for (const t of this.turns) {
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
      const arr = [...map.values()].reverse();
      // Promote everything we just yielded into _filesSeen under the SAME
      // composite key shape used by the `isNew` lookup so the pulse animation
      // only fires once per (tool, path) pair.
      for (const key of seenKeys) this._filesSeen.add(key);
      return arr;
    },

    get showJumpToLatest() {
      return this.streaming && !this._autoFollow;
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
      if (this.visibleSegments(turn).length) return true;
      // Keep empty assistant turns around while they're still streaming (the
      // thinking indicator renders) or when they carry terminal status text.
      return turn.role === "assistant" && isLast && (turn.streaming || turn.error || turn.aborted);
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
      return s < 60 ? `Thought for ${s}s` : `Thought for ${Math.floor(s / 60)}m ${s % 60}s`;
    },

    fileOpMark(op) {
      switch ((op || "modified").toLowerCase()) {
        case "added": return "+";
        case "deleted": return "−";
        case "renamed": return "→";
        default: return "~";
      }
    },

    todoIcon(status) {
      const s = (status || "pending").toLowerCase();
      if (s === "completed") return "☑";
      if (s === "in_progress") return "◐";
      return "☐";
    },

    // ---------- User actions ------------------------------------------

    canSend() {
      if (!this.draftMessage.trim()) return false;
      if (this.thread) return true;
      return !!(this.draftRepoId && this.draftRef);
    },

    autosize() {
      const el = this.$refs.prompt;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 14 * 16) + "px";
    },

    async submit() {
      if (!this.canSend() || this.streaming || this.resuming) return;

      if (!this.thread) {
        const threadId = uuid();
        this.thread = { thread_id: threadId, repo_id: this.draftRepoId, ref: this.draftRef };
        history.replaceState(null, "", `/dashboard/chat/${threadId}/`);
      }

      this.turns.push({
        id: uuid(),
        role: "user",
        segments: [{ type: "text", content: this.draftMessage }],
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

      // Use whatever the agent picker last dispatched (it dispatches both on user change
      // and once on init for the seeded default). The hidden-input fallback covers the
      // edge case where the picker hasn't dispatched yet at submit time (very fast first
      // click). The server pins these to ``ChatThread.agent_model`` / ``agent_thinking_level``
      // on first sight of the thread and ignores them on subsequent turns.
      const agentModel = this._agentModel
        || this.$root?.querySelector?.('input[name="agent_model"]')?.value
        || "";
      const agentThinkingLevel = this._agentThinkingLevel
        || this.$root?.querySelector?.('input[name="agent_thinking_level"]')?.value
        || "";
      const forwardedProps = {};
      if (agentModel) forwardedProps.agent_model = agentModel;
      if (agentThinkingLevel) forwardedProps.agent_thinking_level = agentThinkingLevel;

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
          assistantTurn.error = await formatHttpError(resp);
          return;
        }

        // The run now executes server-side detached from any connection; all
        // event consumption goes through the resumable relay stream.
        reason = await this._streamRun(this._activeRun, assistantTurn);
      } catch (err) {
        console.error("chat: failed to start run", err);
        assistantTurn.error = "Connection lost — please retry.";
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
        await fetch(this.cancelEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": this.csrfToken },
          body: JSON.stringify({
            thread_id: this._activeRun.threadId,
            run_id: this._activeRun.runId,
          }),
          credentials: "include",
        });
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
        if (evt.code === "run_cancelled") {
          turn.aborted = true;
        } else {
          turn.error = evt.message || "Run failed";
        }
        turn.segments.forEach((s) => {
          if ((s.type === "tool_call" || s.type === "publish_phase") && s.status === "running") {
            s.status = "error";
          }
        });
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
          for (const s of t.segments) {
            if (s.type === "publish_phase" && s.status === "running") s.status = "done";
          }
        }
      }
    },

    _appendTextSegment(turn, content) {
      turn.segments.push({ type: "text", content });
      return turn.segments[turn.segments.length - 1];
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
  });

  document.addEventListener("alpine:init", () => {
    window.Alpine.data("chat", chat);
  });
})();
