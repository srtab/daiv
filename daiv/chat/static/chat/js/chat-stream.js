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
  };

  const PATH_TOOLS = new Set(["read_file", "write_file", "edit_file", "grep", "glob", "ls"]);

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

  const pickPath = (argsStr, toolName) => {
    try {
      const args = JSON.parse(argsStr);
      if (!args || typeof args !== "object") return null;
      if (toolName === "grep" || toolName === "glob") return args.pattern ?? args.query ?? args.glob ?? null;
      return args.path ?? args.file_path ?? args.dir ?? null;
    } catch {
      return null;
    }
  };

  const chat = (config) => ({
    endpoint: config.endpoint,
    csrfToken: config.csrfToken || "",
    thread: config.thread,
    turns: loadInitialTurns(),
    draftMessage: "",
    draftRepoId: "",
    draftRef: "main",
    streaming: false,
    abortCtl: null,
    _toolIndex: new Map(),
    _filesSeen: new Set(),
    _scrollQueued: false,
    _autoFollow: true,
    _thinkingTimer: null,
    _scrollListener: null,
    thinkingLabel: THINKING_LABELS[0],
    filesTouchedLimit: 20,

    init() {
      // Seed _filesSeen with any paths already present in hydrated history so
      // the "new row pulse" animation does not fire on initial load.
      for (const t of this.turns) {
        for (const seg of t.segments) {
          if (seg.type !== "tool_call") continue;
          const p = pickPath(seg.args, seg.name);
          if (p) this._filesSeen.add(`${seg.name}::${p}`);
        }
      }

      const onScroll = () => {
        const doc = document.documentElement;
        const distanceFromBottom = doc.scrollHeight - (window.scrollY + window.innerHeight);
        this._autoFollow = distanceFromBottom < 120;
      };
      window.addEventListener("scroll", onScroll, { passive: true });
      this._scrollListener = onScroll;

      this.$watch("streaming", (on) => {
        if (on) {
          let i = 0;
          this.thinkingLabel = THINKING_LABELS[0];
          this._thinkingTimer = setInterval(() => {
            i = (i + 1) % THINKING_LABELS.length;
            this.thinkingLabel = THINKING_LABELS[i];
          }, 1800);
        } else if (this._thinkingTimer) {
          clearInterval(this._thinkingTimer);
          this._thinkingTimer = null;
        }
      });
    },

    destroy() {
      if (this._scrollListener) window.removeEventListener("scroll", this._scrollListener);
      if (this._thinkingTimer) clearInterval(this._thinkingTimer);
    },

    // ---------- Derived getters (right rail) ---------------------------

    get runStatus() {
      const last = this.turns[this.turns.length - 1];
      if (last?.error) return { tone: "error", label: "error" };
      if (!this.streaming) return { tone: "idle", label: "idle" };
      const activeTool = last?.segments.slice().reverse().find(
        (s) => s.type === "tool_call" && s.status === "running",
      );
      if (activeTool) return { tone: "running", label: `running ${activeTool.name}…` };
      return { tone: "thinking", label: this.thinkingLabel };
    },

    get latestTodos() {
      for (let i = this.turns.length - 1; i >= 0; i--) {
        const segs = this.turns[i].segments;
        for (let j = segs.length - 1; j >= 0; j--) {
          const s = segs[j];
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
      const map = new Map(); // path -> { path, segmentId, isNew }
      for (const t of this.turns) {
        for (const seg of t.segments) {
          if (seg.type !== "tool_call" || !PATH_TOOLS.has(seg.name)) continue;
          const path = pickPath(seg.args, seg.name);
          if (!path) continue;
          const key = `${seg.name}::${path}`;
          const isNew = !this._filesSeen.has(key);
          map.set(path, { path, segmentId: `tool-${seg.id}`, isNew });
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

    toolSignature(seg) {
      if (!window.toolSignature) return { label: seg.name, path: "", badges: [] };
      return window.toolSignature(seg.name, seg.args, seg.result, seg.status);
    },

    toolBodyHTML(seg) {
      if (!window.toolBodyHTML) return "";
      return window.toolBodyHTML(seg.name, seg.args, seg.result, seg.status);
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
      if (!this.canSend() || this.streaming) return;

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
      this._autoFollow = true;

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
      } else if (type === AGUI.TOOL_CALL_START) {
        // If a parent task is running and this isn't the task's own id, suppress.
        if (activeTask && evt.toolCallId !== activeTask.id) return;
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
        if (idx != null && turn.segments[idx]) {
          turn.segments[idx].args += evt.delta || "";
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
        window.scrollTo({
          top: document.documentElement.scrollHeight,
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
