// Wire-protocol types — kept in sync with docs/wire-protocol.md and the Pydantic models
// in server-reference/server/models.py. Treat this file as the L1 source of truth for TS.

export const PROTOCOL_VERSION = "1.0";

// --- Content blocks (mirror Anthropic SDK shape) ---

export type TextBlock = { type: "text"; text: string };
export type ImageBlock = {
  type: "image";
  source: { type: "base64"; media_type: string; data: string };
};
export type ToolUseBlock = {
  type: "tool_use";
  id: string;
  name: string;
  input: unknown;
};
export type ToolResultBlock = {
  type: "tool_result";
  tool_use_id: string;
  content: string | ContentBlock[];
  is_error?: boolean;
};
export type ContentBlock = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock;

// --- Inbound (client → server) ---

export type UserInput = string | ContentBlock[];

export type InboundMessage =
  | { type: "user_message"; content: UserInput }
  | { type: "interrupt" }
  | {
      type: "permission_response";
      correlation_id: string;
      behavior: "allow" | "deny";
      updated_input?: Record<string, unknown>;
      updated_permissions?: unknown[];
      message?: string;
      interrupt?: boolean;
    }
  | { type: "question_response"; correlation_id: string; answers: unknown }
  | { type: "set_permission_mode"; mode: string }
  | { type: "set_model"; model: string | null }
  | { type: "stop_task"; task_id: string };

// --- Outbound (server → client) ---

export type ServerEvent =
  | { event: "session_ready"; data: { session_id: string; protocol_version: string } }
  | { event: "message_delta"; data: { message_id: string; delta: ContentBlock | { text: string } } }
  | { event: "message_complete"; data: { message_id: string; message: AssistantMessage } }
  | {
      event: "tool_use";
      data: { message_id: string; tool_use_id: string; tool_name: string; input: unknown };
    }
  | { event: "tool_result"; data: { tool_use_id: string; output: unknown; is_error: boolean } }
  | {
      event: "permission_request";
      data: {
        correlation_id: string;
        tool_name: string;
        input: Record<string, unknown>;
        context?: Record<string, unknown>;
      };
    }
  | {
      event: "ask_user_question";
      data: { correlation_id: string; questions: AskUserQuestionInput };
    }
  | {
      event: "hook_decision_request";
      data: { correlation_id: string; hook_event: string; hook_input: Record<string, unknown> };
    }
  | {
      event: "result";
      data: {
        session_id: string;
        subtype: string;
        total_cost_usd?: number;
        [key: string]: unknown;
      };
    }
  | { event: "error"; data: { code: string; message: string } }
  | { event: "mcp_status_change"; data: { server_name: string; status: string } }
  | { event: "done"; data: Record<string, never> };

export type ServerEventName = ServerEvent["event"];
export type EventOf<N extends ServerEventName> = Extract<ServerEvent, { event: N }>;

// Each delivered event is tagged with its monotonic server-side seq id.
export type DeliveredEvent = ServerEvent & { id: number };

export interface AssistantMessage {
  id: string;
  role: "assistant";
  content: ContentBlock[];
  model?: string;
  stop_reason?: string | null;
}

// AskUserQuestion shape per the SDK's tool input schema.
export interface AskUserQuestionItem {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options: { label: string; description?: string }[];
}
export type AskUserQuestionInput = { questions: AskUserQuestionItem[] };

// Approve/deny payload helpers.
export interface ApproveOptions {
  updatedInput?: Record<string, unknown>;
  updatedPermissions?: unknown[];
}
export interface DenyOptions {
  message?: string;
  interrupt?: boolean;
}

export interface CreateSessionOptions {
  model?: string;
  permission_mode?: string;
  cwd?: string;
}

export interface CreateSessionResponse {
  session_id: string;
  protocol_version: string;
}
