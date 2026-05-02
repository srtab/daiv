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
    statusEndpoint: config.statusEndpoint || "",
    csrfToken: config.csrfToken || "",
    // Hydrate the MR pill from the server-rendered checkpoint. We rebuild the
    // thread object so Alpine tracks `merge_request` as a reactive property
    // from the first render — assigning a *new* key onto an existing reactive
    // proxy after init doesn't always re-render templates.
    thread: config.thread ? { ...config.thread, merge_request: loadInitialMergeRequest() } : null,
    turns: loadInitialTurns(),
    draftMessage: "",
    draftRepoId: "",
    draftRef: "",
    streaming: false,
    resuming: !!config.activeRunId,
    abortCtl: null,
    _toolIndex: new Map(),
    _reasoningIndex: new Map(),
    _filesSeen: new Set(),
    _scrollQueued: false,
    _autoFollow: true,
    _thinkingTimer: null,
    _scrollListener: null,
    _resumePoll: null,
    _thinkingPhrase: THINKING_LABELS[0],
    filesTouchedLimit: 20,
    panel: null,
    usage_summary: (() => {
      try {
        const el = document.getElementById("chat-initial-usage");
        return el ? JSON.parse(el.textContent || "null") : null;
      } catch { return null; }
    })(),

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

      if (this.resuming) {
        // Page was loaded while a run is still executing server-side. The AGUI
        // stream cannot be re-attached to mid-flight, so poll the thread status
        // endpoint and reload once the server-side run releases its slot.
        this._startResumePoll();
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
      if (this._resumePoll) clearTimeout(this._resumePoll);
    },

    _startResumePoll() {
      if (!this.statusEndpoint) return;
      // Hard cap so a stuck server-side slot or a network failure does not poll
      // forever — about 3 minutes at a 3 s cadence, after which we surface a
      // "lost connection" banner and stop. The server-side stale-takeover
      // window is longer, so a refresh after the banner will recover.
      const MAX_FAILURES = 60;
      let failures = 0;
      const fail = (label) => {
        this.resuming = false;
        const last = this.turns[this.turns.length - 1];
        if (last && last.role === "assistant") last.error = label;
        if (this._resumePoll) {
          clearTimeout(this._resumePoll);
          this._resumePoll = null;
        }
      };
      const tick = async () => {
        try {
          const resp = await fetch(this.statusEndpoint, { credentials: "include" });
          if (resp.ok) {
            failures = 0;
            const data = await resp.json();
            if (!data?.active) {
              window.location.reload();
              return;
            }
          } else if (resp.status === 401 || resp.status === 403) {
            fail("Session expired — please refresh and sign in.");
            return;
          } else if (resp.status === 404) {
            fail("This conversation is no longer available.");
            return;
          } else {
            failures += 1;
          }
        } catch {
          failures += 1;
        }
        if (failures >= MAX_FAILURES) {
          fail("Lost connection to the server — refresh to continue.");
          return;
        }
        this._resumePoll = setTimeout(tick, 3000);
      };
      this._resumePoll = setTimeout(tick, 1500);
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

    get fileLineDelta() {
      let added = 0, removed = 0;
      for (const f of this.filesTouched) {
        if (typeof f.added === "number") added += f.added;
        if (typeof f.removed === "number") removed += f.removed;
      }
      return { added, removed };
    },

    formatTokens(n) {
      if (!n) return "0";
      if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
      if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
      return String(n);
    },

    formatCost(c) {
      const v = Number(c);
      if (!Number.isFinite(v)) return "0.00";
      return v.toFixed(2);
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

      const priorMessages = this.turns
        .slice(0, -1)
        .map((t) => ({
          id: t.id,
          role: t.role,
          content: t.segments
            .filter((s) => s.type === "text")
            .map((s) => s.content)
            .join("\n\n"),
        }))
        .filter((m) => m.content);

      const body = {
        threadId: this.thread.thread_id,
        runId: uuid(),
        state: {},
        messages: priorMessages,
        tools: [],
        context: [],
        forwardedProps: {},
      };

      this.draftMessage = "";
      this.$nextTick(() => this.autosize());
      this.streaming = true;
      this.abortCtl = new AbortController();

      try {
        const resp = await fetch(this.endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Repo-ID": this.thread.repo_id,
            "X-Ref": this.thread.ref,
            "X-CSRFToken": this.csrfToken,
          },
          body: JSON.stringify(body),
          credentials: "include",
          signal: this.abortCtl.signal,
        });

        if (!resp.ok) {
          assistantTurn.error = await formatHttpError(resp);
          return;
        }

        await this.consume(resp.body, assistantTurn);
      } catch (err) {
        if (err.name !== "AbortError") {
          console.error("chat: stream failed", err);
          assistantTurn.error = "Connection lost — please retry.";
        } else {
          assistantTurn.aborted = true;
        }
      } finally {
        assistantTurn.streaming = false;
        assistantTurn.segments.forEach((s) => {
          if (s.type === "tool_call" && s.status === "running") s.status = "done";
          if (s.type === "thinking" && s.status === "running") {
            s.status = "done";
            s.endedAt = Date.now();
          }
        });
        this.streaming = false;
        this.abortCtl = null;
        this.scrollToBottom();
      }
    },

    stop() {
      this.abortCtl?.abort();
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

    async consume(stream, turn) {
      const reader = stream.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let evt;
          try {
            evt = JSON.parse(line.slice(5).trim());
          } catch (err) {
            console.error("chat: malformed SSE frame, skipping", line, err);
            continue;
          }
          this.dispatch(evt, turn);
        }
      }
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
        // Phase chips intentionally ignore args (the structured-response JSON
        // is not user-facing).
        if (seg && seg.type === "tool_call" && !seg.sealed) {
          seg.args += evt.delta || "";
        }
      } else if (type === AGUI.TOOL_CALL_END) {
        // Structured-response tool calls don't always trigger a TOOL_CALL_RESULT
        // (the agent extracts the structured payload and stops without
        // executing a tool), so use the END signal — which always fires once
        // args streaming finishes — to flip the phase chip to done.
        const idx = this._toolIndex.get(evt.toolCallId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (seg && seg.type === "publish_phase") {
          seg.status = "done";
        }
      } else if (type === AGUI.TOOL_CALL_RESULT) {
        const idx = this._toolIndex.get(evt.toolCallId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (!seg) return;
        if (seg.type === "publish_phase") {
          seg.status = "done";
        } else {
          seg.result = evt.content;
          seg.status = "done";
        }
      } else if (type === AGUI.RUN_ERROR) {
        turn.error = evt.message || "Run failed";
        turn.segments.forEach((s) => {
          if ((s.type === "tool_call" || s.type === "publish_phase") && s.status === "running") {
            s.status = "error";
          }
        });
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
      } else if (type === "CUSTOM" && evt.name === "chat.usage") {
        this.usage_summary = evt.value;
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
