import type {
  AssistantMessage,
  ContentBlock,
  DeliveredEvent,
  HistoryEntry,
} from "@agent-webkit/core";

// ────────────────────────────────────────────────────────────────────────────
// L2 reducer — keyed-by-session-id state for the multiplex model.
//
// The L1 client opens one persistent /stream that yields events tagged with
// session_id. The L2 reducer keeps a per-session SessionState slot inside a
// MuxState, and routes every event to its slot. Components select a single
// session's state via a thin selector hook.
// ────────────────────────────────────────────────────────────────────────────

export type DisplayMessage =
  | { kind: "user"; id: string; content: string | ContentBlock[] }
  | {
      kind: "assistant";
      id: string;
      message_id: string;
      content: ContentBlock[];
      streaming: boolean;
    }
  | { kind: "tool_result"; id: string; tool_use_id: string; output: unknown; is_error: boolean };

export type Status =
  | "idle"
  | "streaming"
  | "awaiting_permission"
  | "awaiting_question"
  | "awaiting_hook"
  | "error";

export interface PendingPermission {
  correlation_id: string;
  tool_name: string;
  input: Record<string, unknown>;
  context?: Record<string, unknown>;
}

export interface PendingQuestion {
  correlation_id: string;
  questions: { questions: { question: string; header?: string; multiSelect?: boolean; options: { label: string; description?: string }[] }[] };
}

/** Per-session state — what useActiveSession exposes for a single sid. */
export interface SessionState {
  messages: DisplayMessage[];
  status: Status;
  pendingPermission: PendingPermission | null;
  pendingQuestion: PendingQuestion | null;
  lastError: { code: string; message: string } | null;
  totalCostUsd: number;
}

/** Top-level mux state — many sessions, keyed by id. */
export interface MuxState {
  sessions: Record<string, SessionState>;
  /** Most recent global error (e.g. stream connection failure not tied to a session). */
  streamError: { code: string; message: string } | null;
}

export const initialSessionState: SessionState = {
  messages: [],
  status: "idle",
  pendingPermission: null,
  pendingQuestion: null,
  lastError: null,
  totalCostUsd: 0,
};

export const initialMuxState: MuxState = {
  sessions: {},
  streamError: null,
};

export type Action =
  | {
      type: "local_user_message";
      sessionId: string;
      content: string | ContentBlock[];
      localId: string;
    }
  | { type: "server_event"; event: DeliveredEvent }
  | {
      type: "history_loaded";
      sessionId: string;
      events: HistoryEntry[];
    }
  | { type: "ensure_session"; sessionId: string }
  | { type: "remove_session"; sessionId: string }
  | { type: "permission_resolved"; sessionId: string; correlationId: string }
  | { type: "question_resolved"; sessionId: string; correlationId: string }
  | { type: "stream_error"; error: { code: string; message: string } };

function appendDelta(blocks: ContentBlock[], delta: unknown): ContentBlock[] {
  if (delta && typeof delta === "object") {
    const d = delta as { type?: string; text?: string };
    if (d.type === undefined && typeof d.text === "string") {
      const last = blocks[blocks.length - 1];
      if (last && last.type === "text") {
        return [...blocks.slice(0, -1), { type: "text", text: last.text + d.text }];
      }
      return [...blocks, { type: "text", text: d.text }];
    }
    if (d.type === "text" && typeof d.text === "string") {
      const last = blocks[blocks.length - 1];
      if (last && last.type === "text") {
        return [...blocks.slice(0, -1), { type: "text", text: last.text + d.text }];
      }
      return [...blocks, { type: "text", text: d.text }];
    }
    return [...blocks, delta as ContentBlock];
  }
  return blocks;
}

function getOrInit(state: MuxState, sid: string): SessionState {
  return state.sessions[sid] ?? initialSessionState;
}

function set(state: MuxState, sid: string, next: SessionState): MuxState {
  return { ...state, sessions: { ...state.sessions, [sid]: next } };
}

/**
 * Apply one server event to a single session's state. Mirrors the
 * pre-multiplex reducer behavior, but operates on a single SessionState.
 */
function reduceSession(s: SessionState, event: string, data: any, seqId: number): SessionState {
  switch (event) {
    case "session_ready":
      return s;

    case "user_message": {
      const content = data.content;
      // Dedupe against optimistic local insert from local_user_message.
      const last = s.messages[s.messages.length - 1];
      if (
        last &&
        last.kind === "user" &&
        JSON.stringify(last.content) === JSON.stringify(content)
      ) {
        return s;
      }
      return {
        ...s,
        messages: [...s.messages, { kind: "user", id: `srv-${seqId}`, content }],
      };
    }

    case "message_delta": {
      const { message_id, delta } = data;
      const idx = s.messages.findIndex(
        (m) => m.kind === "assistant" && m.message_id === message_id
      );
      if (idx === -1) {
        const newMsg: DisplayMessage = {
          kind: "assistant",
          id: message_id,
          message_id,
          content: appendDelta([], delta),
          streaming: true,
        };
        return { ...s, messages: [...s.messages, newMsg], status: "streaming" };
      }
      const existing = s.messages[idx]!;
      if (existing.kind !== "assistant") return s;
      const updated: DisplayMessage = {
        ...existing,
        content: appendDelta(existing.content, delta),
        streaming: true,
      };
      const messages = [...s.messages];
      messages[idx] = updated;
      return { ...s, messages, status: "streaming" };
    }

    case "message_complete": {
      const { message_id, message } = data;
      const idx = s.messages.findIndex(
        (m) => m.kind === "assistant" && m.message_id === message_id
      );
      const reconciled: DisplayMessage = {
        kind: "assistant",
        id: message_id,
        message_id,
        content: (message as AssistantMessage).content,
        streaming: false,
      };
      if (idx === -1) {
        return { ...s, messages: [...s.messages, reconciled] };
      }
      const messages = [...s.messages];
      messages[idx] = reconciled;
      return { ...s, messages };
    }

    case "tool_use":
      return s;

    case "tool_result": {
      const { tool_use_id, output, is_error } = data;
      return {
        ...s,
        messages: [
          ...s.messages,
          { kind: "tool_result", id: `tr-${tool_use_id}`, tool_use_id, output, is_error },
        ],
      };
    }

    case "permission_request":
      return {
        ...s,
        pendingPermission: {
          correlation_id: data.correlation_id,
          tool_name: data.tool_name,
          input: data.input,
          ...(data.context !== undefined ? { context: data.context } : {}),
        },
        status: "awaiting_permission",
      };

    case "ask_user_question":
      return {
        ...s,
        pendingQuestion: {
          correlation_id: data.correlation_id,
          questions: data.questions,
        },
        status: "awaiting_question",
      };

    case "hook_decision_request":
      return { ...s, status: "awaiting_hook" };

    case "result": {
      const cost = typeof data.total_cost_usd === "number" ? data.total_cost_usd : 0;
      return { ...s, totalCostUsd: s.totalCostUsd + cost, status: "idle" };
    }

    case "error":
      return { ...s, status: "error", lastError: data };

    case "mcp_status_change":
      return s;

    case "done":
      return { ...s, status: "idle" };
  }
  return s;
}

export function reduce(state: MuxState, action: Action): MuxState {
  switch (action.type) {
    case "ensure_session": {
      if (state.sessions[action.sessionId]) return state;
      return set(state, action.sessionId, initialSessionState);
    }

    case "remove_session": {
      if (!(action.sessionId in state.sessions)) return state;
      const next = { ...state.sessions };
      delete next[action.sessionId];
      return { ...state, sessions: next };
    }

    case "local_user_message": {
      const s = getOrInit(state, action.sessionId);
      return set(state, action.sessionId, {
        ...s,
        messages: [
          ...s.messages,
          { kind: "user", id: action.localId, content: action.content },
        ],
        status: "streaming",
      });
    }

    case "server_event": {
      const ev = action.event;
      const sid = ev.session_id;
      if (!sid) return state;
      const cur = getOrInit(state, sid);
      const next = reduceSession(cur, ev.event, ev.data as any, ev.id);
      if (next === cur) return state;
      return set(state, sid, next);
    }

    case "history_loaded": {
      // Replay the history through reduceSession to populate the per-session
      // state slot with past messages. seq for history entries is synthetic.
      let s = state.sessions[action.sessionId] ?? initialSessionState;
      let seq = -1;
      for (const e of action.events) {
        s = reduceSession(s, e.event, e.payload as any, seq);
        seq -= 1;
      }
      // History is a snapshot — coming back from past, so reset status to idle.
      return set(state, action.sessionId, { ...s, status: "idle" });
    }

    case "permission_resolved": {
      const s = state.sessions[action.sessionId];
      if (!s || s.pendingPermission?.correlation_id !== action.correlationId) return state;
      return set(state, action.sessionId, { ...s, pendingPermission: null, status: "streaming" });
    }

    case "question_resolved": {
      const s = state.sessions[action.sessionId];
      if (!s || s.pendingQuestion?.correlation_id !== action.correlationId) return state;
      return set(state, action.sessionId, { ...s, pendingQuestion: null, status: "streaming" });
    }

    case "stream_error":
      return { ...state, streamError: action.error };

    default:
      return state;
  }
}
