import {
  Component,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import type { ReactNode, KeyboardEvent } from "react";
import {
  CopilotKitProvider,
  useAgent,
  useCopilotKit,
  useDefaultRenderTool,
} from "@copilotkit/react-core/v2";
import { HttpAgent } from "@ag-ui/client";
import type {
  AssistantMessage,
  Message,
  ToolCall,
  ToolMessage,
} from "@ag-ui/client";
import ReactMarkdown from "react-markdown";
import { readCsrfToken, type MountConfig } from "./config";
import { MergeRequestCard } from "./MergeRequestCard";
import { useDaivState } from "./state/coagent";
import { useRunStatus } from "./use_run_status";
import { renderTool } from "./tools";
import type { ToolStatus } from "./tools/types";
import { markdownComponents } from "./markdown";

const AGENT_ID = "DAIV";

function mapStatus(s: string): ToolStatus {
  if (s === "complete") return "complete";
  if (s === "error" || s === "failed") return "error";
  return "running";
}

class ChatErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error) {
    console.error("ChatErrorBoundary caught:", error);
  }
  render() {
    if (this.state.error) {
      return (
        <div className="chat-error-banner" role="alert">
          Chat crashed: {this.state.error.message}. Refresh to retry.
        </div>
      );
    }
    return this.props.children;
  }
}

function parseArgs(raw: string | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function userText(msg: Message): string {
  if (msg.role !== "user") return "";
  const content = msg.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((p): p is { type: "text"; text: string } => p?.type === "text")
      .map((p) => p.text)
      .join("\n");
  }
  return "";
}

function ToolCallSegment({
  toolCall,
  result,
  finished,
}: {
  toolCall: ToolCall;
  result: ToolMessage | undefined;
  finished: boolean;
}) {
  const status: ToolStatus = result
    ? mapStatus(result.role === "tool" ? "complete" : "complete")
    : finished
      ? "error"
      : "running";
  return (
    <div className="chat-segment chat-segment--tool_call">
      {renderTool({
        name: toolCall.function.name,
        args: parseArgs(toolCall.function.arguments),
        argsRaw: toolCall.function.arguments,
        status,
        result: result?.content,
      })}
    </div>
  );
}

function AssistantTurn({
  message,
  toolResults,
  isLastAssistant,
  running,
}: {
  message: AssistantMessage;
  toolResults: Map<string, ToolMessage>;
  isLastAssistant: boolean;
  running: boolean;
}) {
  const hasText = !!message.content && message.content.trim().length > 0;
  const toolCalls = message.toolCalls ?? [];
  return (
    <article className="chat-turn chat-turn--assistant">
      <div className="chat-turn__body">
        {hasText && (
          <div className="chat-segment chat-segment--text">
            <div className="chat-text">
              <ReactMarkdown components={markdownComponents}>
                {message.content ?? ""}
              </ReactMarkdown>
            </div>
          </div>
        )}
        {toolCalls.map((tc: ToolCall) => (
          <ToolCallSegment
            key={tc.id}
            toolCall={tc}
            result={toolResults.get(tc.id)}
            finished={!isLastAssistant || !running}
          />
        ))}
      </div>
    </article>
  );
}

function UserTurn({ message }: { message: Message }) {
  return (
    <article className="chat-turn chat-turn--user">
      <div className="chat-turn__body">
        <div className="chat-segment chat-segment--text">
          <span className="chat-text">{userText(message)}</span>
        </div>
      </div>
    </article>
  );
}

function buildToolResults(messages: ReadonlyArray<Message>): Map<string, ToolMessage> {
  const map = new Map<string, ToolMessage>();
  for (const m of messages) {
    if (m.role === "tool" && m.toolCallId) map.set(m.toolCallId, m);
  }
  return map;
}

function Composer({
  onSubmit,
  onStop,
  running,
}: {
  onSubmit: (text: string) => void;
  onStop: () => void;
  running: boolean;
}) {
  const [value, setValue] = useState("");
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const autosize = useCallback(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
  }, []);

  useEffect(autosize, [value, autosize]);

  const send = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || running) return;
    onSubmit(trimmed);
    setValue("");
  }, [value, running, onSubmit]);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      send();
    }
  };

  return (
    <form
      className={`chat-composer${running ? " chat-composer--sending" : ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <label htmlFor="chat_prompt" className="sr-only">
        Message DAIV
      </label>
      <textarea
        id="chat_prompt"
        ref={taRef}
        rows={2}
        value={value}
        disabled={running}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Describe the change, the bug, or what to explore…"
        className="chat-composer__textarea"
      />
      <div className="chat-composer__actions">
        <div className="chat-composer__hint">
          <kbd className="chat-composer__kbd">⌘</kbd>
          <kbd className="chat-composer__kbd">↵</kbd>
          <span>to send</span>
        </div>
        <div style={{ display: "inline-flex", gap: 8 }}>
          {running && (
            <button
              type="button"
              onClick={onStop}
              className="chat-composer__btn chat-composer__btn--stop"
            >
              Stop
            </button>
          )}
          <button
            type="submit"
            disabled={running || value.trim().length === 0}
            className="chat-composer__btn chat-composer__btn--send"
          >
            <span>{running ? "Sending…" : "Send"}</span>
          </button>
        </div>
      </div>
    </form>
  );
}

function DaivChat({ threadId }: { threadId: string }) {
  const { agent } = useAgent({ agentId: AGENT_ID, threadId });
  const { copilotkit } = useCopilotKit();
  const [, forceUpdate] = useReducer((x: number) => x + 1, 0);
  const [running, setRunning] = useState(false);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const { active } = useRunStatus(`/api/chat/threads/${threadId}/status`);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  useDefaultRenderTool({
    render: ({ name, parameters, status, result }) =>
      renderTool({
        name,
        args: (parameters ?? {}) as Record<string, unknown>,
        status: mapStatus(String(status)),
        result,
      }),
  });

  useEffect(() => {
    if (!agent) return;
    const sub = agent.subscribe({
      onMessagesChanged: () => forceUpdate(),
      onStateChanged: () => forceUpdate(),
      onRunStartedEvent: () => {
        setRunning(true);
        setAgentError(null);
      },
      onRunFinishedEvent: () => setRunning(false),
      onRunErrorEvent: ({ event }) => {
        setRunning(false);
        const msg = event.message ?? "Unknown agent error";
        const code = event.code ?? "";
        if (code === "409" || /already.*progress/i.test(msg)) setBlocked(true);
        else setAgentError(msg);
      },
    });
    return () => sub.unsubscribe();
  }, [agent]);

  useEffect(() => {
    if (blocked && !active) setBlocked(false);
  }, [blocked, active]);

  useEffect(() => {
    const el = transcriptRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  });

  const messages = (agent?.messages ?? []) as ReadonlyArray<Message>;
  const toolResults = useMemo(() => buildToolResults(messages), [messages]);

  let lastAssistantIdx = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "assistant") {
      lastAssistantIdx = i;
      break;
    }
  }

  const submit = useCallback(
    async (text: string) => {
      if (!agent) return;
      agent.addMessage({
        id: crypto.randomUUID(),
        role: "user",
        content: text,
      });
      try {
        await copilotkit.runAgent({ agent });
      } catch (err) {
        console.error("DaivChat: runAgent failed", err);
      }
    },
    [agent, copilotkit],
  );

  const stop = useCallback(() => {
    if (!agent) return;
    try {
      copilotkit.stopAgent({ agent });
    } catch (err) {
      console.error("DaivChat: stopAgent failed", err);
    }
  }, [agent, copilotkit]);

  return (
    <div className="chat-layout">
      {blocked && (
        <div className="chat-blocked-banner">Another run is in progress…</div>
      )}
      {agentError && (
        <div className="chat-error-banner" role="alert">
          {agentError}
          <button type="button" onClick={() => setAgentError(null)}>
            Dismiss
          </button>
        </div>
      )}
      <div ref={transcriptRef} className="chat-transcript">
        {messages.map((m, i) => {
          if (m.role === "user") return <UserTurn key={m.id ?? i} message={m} />;
          if (m.role === "assistant") {
            return (
              <AssistantTurn
                key={m.id ?? i}
                message={m}
                toolResults={toolResults}
                isLastAssistant={i === lastAssistantIdx}
                running={running}
              />
            );
          }
          return null;
        })}
        {running && (
          <div className="chat-thinking" aria-live="polite">
            <span className="chat-thinking__dot"></span>
            <span>Thinking…</span>
          </div>
        )}
      </div>
      <Composer onSubmit={submit} onStop={stop} running={running} />
    </div>
  );
}

function ChatBody({ threadId }: { threadId: string }) {
  const { state } = useDaivState();
  return (
    <>
      <DaivChat threadId={threadId} />
      <MergeRequestCard mr={state.merge_request ?? null} />
    </>
  );
}

export function Chat({ cfg }: { cfg: MountConfig }) {
  const agent = useMemo(
    () =>
      new HttpAgent({
        url: "/api/chat/completions",
        threadId: cfg.threadId,
      }),
    [cfg.threadId],
  );
  const headers = useMemo(
    () => ({
      "X-Repo-ID": cfg.repoId,
      "X-Ref": cfg.ref,
      "X-CSRFToken": cfg.csrfToken || readCsrfToken(),
    }),
    [cfg.repoId, cfg.ref, cfg.csrfToken],
  );
  return (
    <ChatErrorBoundary>
      <CopilotKitProvider
        selfManagedAgents={{ [AGENT_ID]: agent }}
        headers={headers}
      >
        <ChatBody threadId={cfg.threadId} />
      </CopilotKitProvider>
    </ChatErrorBoundary>
  );
}
