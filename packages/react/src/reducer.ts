import type {
  AssistantMessage,
  ContentBlock,
  DeliveredEvent,
  ServerEvent,
} from "@agent-webkit/core";

// --- Reducer state ---
//
// We hold a flat list of "displayed messages": user messages we sent, and assistant
// messages reconciled from delta + complete events (keyed by message_id).
//
// Tool uses and tool results are attached as blocks within their owning message
// for rendering; we don't try to imitate the SDK's internal block-stream nuances.

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

export interface AgentState {
  messages: DisplayMessage[];
  status: Status;
  pendingPermission: PendingPermission | null;
  pendingQuestion: PendingQuestion | null;
  lastError: { code: string; message: string } | null;
  totalCostUsd: number;
}

export const initialState: AgentState = {
  messages: [],
  status: "idle",
  pendingPermission: null,
  pendingQuestion: null,
  lastError: null,
  totalCostUsd: 0,
};

export type Action =
  | { type: "local_user_message"; content: string | ContentBlock[]; localId: string }
  | { type: "server_event"; event: DeliveredEvent }
  | { type: "permission_resolved"; correlationId: string }
  | { type: "question_resolved"; correlationId: string };

function appendDelta(blocks: ContentBlock[], delta: unknown): ContentBlock[] {
  // The SDK emits deltas as either a partial ContentBlock or a `{text}` chunk.
  // We treat a `{text}` chunk as appending to the last text block (or creating one).
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
    // tool_use / image deltas: append as-is
    return [...blocks, delta as ContentBlock];
  }
  return blocks;
}

export function reduce(state: AgentState, action: Action): AgentState {
  switch (action.type) {
    case "local_user_message":
      return {
        ...state,
        messages: [
          ...state.messages,
          { kind: "user", id: action.localId, content: action.content },
        ],
        status: "streaming",
      };

    case "server_event": {
      const ev: ServerEvent = action.event;
      switch (ev.event) {
        case "session_ready":
          return state;

        case "message_delta": {
          const { message_id, delta } = ev.data;
          const idx = state.messages.findIndex(
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
            return { ...state, messages: [...state.messages, newMsg], status: "streaming" };
          }
          const existing = state.messages[idx]!;
          if (existing.kind !== "assistant") return state;
          const updated: DisplayMessage = {
            ...existing,
            content: appendDelta(existing.content, delta),
            streaming: true,
          };
          const messages = [...state.messages];
          messages[idx] = updated;
          return { ...state, messages, status: "streaming" };
        }

        case "message_complete": {
          const { message_id, message } = ev.data;
          const idx = state.messages.findIndex(
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
            return { ...state, messages: [...state.messages, reconciled] };
          }
          const messages = [...state.messages];
          messages[idx] = reconciled;
          return { ...state, messages };
        }

        case "tool_use":
          // We rely on `message_complete` to surface tool_use blocks in their final form;
          // delta-stream tool_use is best-effort already handled in message_delta.
          return state;

        case "tool_result": {
          const { tool_use_id, output, is_error } = ev.data;
          return {
            ...state,
            messages: [
              ...state.messages,
              {
                kind: "tool_result",
                id: `tr-${tool_use_id}`,
                tool_use_id,
                output,
                is_error,
              },
            ],
          };
        }

        case "permission_request":
          return {
            ...state,
            pendingPermission: {
              correlation_id: ev.data.correlation_id,
              tool_name: ev.data.tool_name,
              input: ev.data.input,
              ...(ev.data.context !== undefined ? { context: ev.data.context } : {}),
            },
            status: "awaiting_permission",
          };

        case "ask_user_question":
          return {
            ...state,
            pendingQuestion: {
              correlation_id: ev.data.correlation_id,
              questions: ev.data.questions,
            },
            status: "awaiting_question",
          };

        case "hook_decision_request":
          // v1 stub — surface via status only.
          return { ...state, status: "awaiting_hook" };

        case "result": {
          const cost = typeof ev.data.total_cost_usd === "number" ? ev.data.total_cost_usd : 0;
          return {
            ...state,
            totalCostUsd: state.totalCostUsd + cost,
            status: "idle",
          };
        }

        case "error":
          return { ...state, status: "error", lastError: ev.data };

        case "mcp_status_change":
          return state;

        case "done":
          return { ...state, status: "idle" };
      }
      return state;
    }

    case "permission_resolved":
      if (state.pendingPermission?.correlation_id !== action.correlationId) return state;
      return { ...state, pendingPermission: null, status: "streaming" };

    case "question_resolved":
      if (state.pendingQuestion?.correlation_id !== action.correlationId) return state;
      return { ...state, pendingQuestion: null, status: "streaming" };

    default:
      return state;
  }
}
