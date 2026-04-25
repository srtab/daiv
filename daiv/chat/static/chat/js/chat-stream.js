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
    thread: config.thread,
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
      const tick = async () => {
        try {
          const resp = await fetch(this.statusEndpoint, { credentials: "include" });
          if (resp.ok) {
            const data = await resp.json();
            if (!data?.active) {
              // Rehydrate turns from the freshly-written checkpoint.
              window.location.reload();
              return;
            }
          }
        } catch {
          /* transient — keep polling */
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
      const record = (path, op, seg, extra = {}) => {
        if (!path) return;
        const key = `${seg.name}::${path}`;
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
      // After computing, promote newly-seen files into the "seen" set so their
      // pulse animation only fires once.
      for (const f of arr) this._filesSeen.add(`${f.path}`);
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
      this.autosize();
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

    _runningTaskSegment(turn) {
      // Returns the still-running `task` tool segment in this turn, or null.
      // While one exists, subagent-internal tool/text frames bleed through the
      // outer AGUI stream — we suppress them so the rendered turn matches what
      // build_turns produces on refresh (a single collapsed task card).
      for (const s of turn.segments) {
        if (s.type === "tool_call" && s.name === "task" && s.status === "running") return s;
      }
      return null;
    },

    dispatch(evt, turn) {
      const type = evt.type;
      const activeTask = this._runningTaskSegment(turn);

      if (type === AGUI.TEXT_MESSAGE_START) {
        if (activeTask) return;
        this._appendTextSegment(turn, "");
      } else if (type === AGUI.TEXT_MESSAGE_CONTENT || type === AGUI.TEXT_MESSAGE_CHUNK) {
        if (activeTask) return;
        const delta = evt.delta || evt.content || "";
        const last = turn.segments[turn.segments.length - 1];
        if (!last || last.type !== "text") {
          this._appendTextSegment(turn, delta);
        } else {
          last.content += delta;
        }
      } else if (type === AGUI.REASONING_START) {
        if (activeTask) return;
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
        if (activeTask) return;
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
        // If a parent task is running and this isn't the task's own id, suppress.
        if (activeTask && evt.toolCallId !== activeTask.id) return;
        // ag_ui_langgraph re-emits START/ARGS/END from OnToolEnd whenever its
        // `has_function_streaming` flag got reset to False by an inner tool
        // completion — which happens for every parent tool that wraps a
        // subagent. Dedupe by tool_call_id and seal the existing segment so
        // the follow-up ARGS delta does not double-serialize the input.
        const existingIdx = this._toolIndex.get(evt.toolCallId);
        if (existingIdx != null && turn.segments[existingIdx]) {
          turn.segments[existingIdx].sealed = true;
          return;
        }
        turn.segments.push({
          type: "tool_call",
          id: evt.toolCallId,
          name: evt.toolCallName,
          args: "",
          result: null,
          status: "running",
        });
        this._toolIndex.set(evt.toolCallId, turn.segments.length - 1);
      } else if (type === AGUI.TOOL_CALL_ARGS) {
        const idx = this._toolIndex.get(evt.toolCallId);
        const seg = idx != null ? turn.segments[idx] : null;
        if (seg && !seg.sealed) {
          seg.args += evt.delta || "";
        }
      } else if (type === AGUI.TOOL_CALL_RESULT) {
        const idx = this._toolIndex.get(evt.toolCallId);
        if (idx != null && turn.segments[idx]) {
          turn.segments[idx].result = evt.content;
          turn.segments[idx].status = "done";
        }
      } else if (type === AGUI.RUN_ERROR) {
        turn.error = evt.message || "Run failed";
        turn.segments.forEach((s) => {
          if (s.type === "tool_call" && s.status === "running") s.status = "error";
        });
      }
      this.scrollToBottom();
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
