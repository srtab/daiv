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

  const uuid = () => crypto.randomUUID();

  const escapeHtml = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

  const renderMessage = (m) => {
    // Markup kept in lockstep with daiv/chat/templates/chat/_message.html — if you restyle
    // the partial, mirror the same classes here so server-rendered history matches live
    // messages.
    const roleLabel = m.role === "user" ? "You" : m.role === "assistant" ? "DAIV" : m.role;
    let html = `<article class="msg msg--${escapeHtml(m.role)} rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3">
      <header class="msg__header mb-1 text-xs uppercase tracking-wide text-gray-500">${escapeHtml(roleLabel)}</header>
      <div class="msg__content prose prose-invert max-w-none text-[15px] text-gray-100">${escapeHtml(m.content).replaceAll("\n", "<br>")}</div>`;
    if (m.tool_calls && m.tool_calls.length) {
      html += `<div class="msg__tools mt-3 space-y-2">`;
      for (const tc of m.tool_calls) {
        html += `<details class="tool-call rounded-lg border border-white/[0.06] bg-white/[0.03] px-3 py-2 text-sm">
          <summary class="flex cursor-pointer items-center gap-2 text-gray-300"><span class="font-medium text-violet-300">${escapeHtml(tc.name || "")}</span></summary>
          ${tc.args ? `<pre class="mt-2 whitespace-pre-wrap text-xs text-gray-400">${escapeHtml(tc.args)}</pre>` : ""}
          ${tc.result ? `<pre class="mt-2 whitespace-pre-wrap text-xs text-gray-300">${escapeHtml(tc.result)}</pre>` : ""}
        </details>`;
      }
      html += `</div>`;
    }
    if (m.error) html += `<p class="msg__error mt-2 text-sm text-red-400">${escapeHtml(m.error)}</p>`;
    html += `</article>`;
    return html;
  };

  const loadInitialMessages = () => {
    const el = document.getElementById("chat-initial-messages");
    if (!el) return [];
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      console.error("chat: failed to parse server-embedded transcript", err);
      return [];
    }
  };

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
      /* fall through to status-only message */
    }
    return `Request failed (status ${resp.status}). Please retry.`;
  };

  const chat = (config) => ({
    endpoint: config.endpoint,
    thread: config.thread,
    messages: loadInitialMessages(),
    draftMessage: "",
    draftRepoId: "",
    draftRef: "main",
    streaming: false,
    error: null,
    abortCtl: null,
    _toolCallIndex: new Map(),
    _scrollQueued: false,

    canSend() {
      if (!this.draftMessage.trim()) return false;
      if (this.thread) return true;
      return !!(this.draftRepoId && this.draftRef);
    },

    async submit() {
      if (!this.canSend() || this.streaming) return;
      this.error = null;

      if (!this.thread) {
        const threadId = uuid();
        this.thread = { thread_id: threadId, repo_id: this.draftRepoId, ref: this.draftRef };
        history.replaceState(null, "", `/dashboard/chat/${threadId}/`);
      }

      const userMsg = { id: uuid(), role: "user", content: this.draftMessage };
      this.messages.push(userMsg);
      const assistantMsg = { id: uuid(), role: "assistant", content: "", tool_calls: [] };
      this.messages.push(assistantMsg);
      this._toolCallIndex.clear();

      const body = {
        threadId: this.thread.thread_id,
        runId: uuid(),
        state: {},
        messages: this.messages
          .slice(0, -1)
          .map((m) => ({ id: m.id, role: m.role, content: m.content })),
        tools: [],
        context: [],
        forwardedProps: {},
      };

      this.draftMessage = "";
      this.streaming = true;
      this.abortCtl = new AbortController();

      try {
        const resp = await fetch(this.endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Repo-ID": this.thread.repo_id,
            "X-Ref": this.thread.ref,
            "X-CSRFToken": document.cookie.match(/csrftoken=([^;]+)/)?.[1] || "",
          },
          body: JSON.stringify(body),
          credentials: "include",
          signal: this.abortCtl.signal,
        });

        if (!resp.ok) {
          assistantMsg.error = await formatHttpError(resp);
          return;
        }

        await this.consume(resp.body, assistantMsg);
      } catch (err) {
        if (err.name !== "AbortError") {
          console.error("chat: stream failed", err);
          assistantMsg.error = "Connection lost — please retry.";
        }
      } finally {
        this.streaming = false;
        this.abortCtl = null;
        this.scrollToBottom();
      }
    },

    stop() {
      this.abortCtl?.abort();
    },

    async consume(stream, assistantMsg) {
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
          this.dispatch(evt, assistantMsg);
        }
      }
    },

    dispatch(evt, assistantMsg) {
      switch (evt.type) {
        case AGUI.TEXT_MESSAGE_CONTENT:
        case AGUI.TEXT_MESSAGE_CHUNK:
          assistantMsg.content += evt.delta || evt.content || "";
          break;
        case AGUI.TOOL_CALL_START: {
          const tc = { id: evt.toolCallId, name: evt.toolCallName, args: "" };
          assistantMsg.tool_calls.push(tc);
          this._toolCallIndex.set(evt.toolCallId, tc);
          break;
        }
        case AGUI.TOOL_CALL_ARGS: {
          const tc = this._toolCallIndex.get(evt.toolCallId);
          if (tc) tc.args += evt.delta || "";
          break;
        }
        case AGUI.TOOL_CALL_RESULT: {
          const tc = this._toolCallIndex.get(evt.toolCallId);
          if (tc) tc.result = evt.content;
          break;
        }
        case AGUI.RUN_ERROR:
          assistantMsg.error = evt.message || "Run failed";
          break;
        default:
          break;
      }
      this.scrollToBottom();
    },

    scrollToBottom() {
      if (this._scrollQueued) return;
      this._scrollQueued = true;
      requestAnimationFrame(() => {
        this._scrollQueued = false;
        const el = this.$refs.transcript;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    renderMessage,
  });

  document.addEventListener("alpine:init", () => {
    window.Alpine.data("chat", chat);
  });
})();
