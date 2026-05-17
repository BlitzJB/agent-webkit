import type {
  ApproveOptions,
  CreateSessionOptions,
  CreateSessionResponse,
  DeliveredEvent,
  DenyOptions,
  HistoryResponse,
  InboundMessage,
  SessionListResponse,
  UserInput,
} from "./types.js";
import { Transport, TransportError } from "./transport.js";

// ────────────────────────────────────────────────────────────────────────────
// L1 SDK — flat, multiplexed client.
//
// One client per process. One persistent SSE stream (`GET /stream`) that
// fans in every wire event from every session, tagged by `session_id`.
// Action methods take a session id; there's no per-session client object.
// ────────────────────────────────────────────────────────────────────────────

export interface AgentClient {
  /** List every persisted session (server-side metadata). */
  listSessions(): Promise<SessionListResponse>;
  /** POST /sessions. Returns the new session id + protocol version. */
  createSession(opts?: CreateSessionOptions): Promise<CreateSessionResponse>;
  /** DELETE /sessions/{id} — purges metadata + transcript replay history. */
  deleteSession(sessionId: string): Promise<void>;
  /** Past wire events for a session — call this on first-attach to populate UI. */
  history(sessionId: string): Promise<HistoryResponse>;
  /**
   * Async-iterable of typed events across ALL sessions, tagged with
   * `session_id`. Auto-reconnects with Last-Event-ID through transient
   * drops. Cancellable via `signal`.
   */
  events(opts?: { signal?: AbortSignal; resumeFromEventId?: string }): AsyncIterable<DeliveredEvent>;
  // Action methods — all take a session id.
  send(sessionId: string, input: UserInput): Promise<void>;
  interrupt(sessionId: string): Promise<void>;
  approve(sessionId: string, correlationId: string, options?: ApproveOptions): Promise<void>;
  deny(sessionId: string, correlationId: string, options?: DenyOptions): Promise<void>;
  answer(sessionId: string, correlationId: string, answers: unknown): Promise<void>;
  setPermissionMode(sessionId: string, mode: string): Promise<void>;
  setModel(sessionId: string, model: string | null): Promise<void>;
  stopTask(sessionId: string, taskId: string): Promise<void>;
  /** Last seq id seen on the multiplexed stream — useful for caller-side resume. */
  readonly lastEventId: string | undefined;
}

export interface CreateAgentClientOptions {
  baseUrl: string;
  token?: string | undefined;
  fetchImpl?: typeof fetch | undefined;
}

// Internal envelope shape that /stream delivers inside each event's `data`.
interface MuxEnvelope {
  session_id: string;
  payload: unknown;
}

export function createAgentClient(opts: CreateAgentClientOptions): AgentClient {
  const transport = new Transport(opts);
  let lastEventId: string | undefined;

  async function input(sessionId: string, msg: InboundMessage): Promise<void> {
    const res = await transport.post(
      `/sessions/${encodeURIComponent(sessionId)}/input`,
      msg,
    );
    if (res.status === 204) return;
    if (res.status === 409) {
      const text = await res.text().catch(() => "");
      throw new TransportError("Conflict: another subscriber already replied", 409, text);
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new TransportError(`Input rejected: ${res.status}`, res.status, text);
    }
  }

  return {
    get lastEventId(): string | undefined {
      return lastEventId;
    },

    async listSessions(): Promise<SessionListResponse> {
      return await transport.getJSON<SessionListResponse>("/sessions");
    },

    async createSession(createOpts?: CreateSessionOptions): Promise<CreateSessionResponse> {
      return await transport.postJSON<CreateSessionResponse>("/sessions", createOpts ?? {});
    },

    async deleteSession(sessionId: string): Promise<void> {
      await transport.delete(`/sessions/${encodeURIComponent(sessionId)}`);
    },

    async history(sessionId: string): Promise<HistoryResponse> {
      return await transport.getJSON<HistoryResponse>(
        `/sessions/${encodeURIComponent(sessionId)}/history`,
      );
    },

    async *events(opts?: { signal?: AbortSignal; resumeFromEventId?: string }): AsyncIterable<DeliveredEvent> {
      const streamOpts: { signal?: AbortSignal; lastEventId?: string } = {};
      if (opts?.signal) streamOpts.signal = opts.signal;
      const startFrom = opts?.resumeFromEventId ?? lastEventId;
      if (startFrom !== undefined) streamOpts.lastEventId = startFrom;

      for await (const raw of transport.streamEvents("/stream", streamOpts)) {
        if (raw.id !== undefined) lastEventId = raw.id;
        const eventName = raw.event ?? "message";
        let envelope: MuxEnvelope | null = null;
        try {
          const parsed = raw.data === "" ? null : JSON.parse(raw.data);
          if (parsed && typeof parsed === "object" && "session_id" in parsed && "payload" in parsed) {
            envelope = parsed as MuxEnvelope;
          }
        } catch {
          // Fall through — surface as an error event below.
        }
        const seq = raw.id !== undefined ? Number(raw.id) : -1;
        if (envelope === null) {
          yield {
            id: seq,
            session_id: "",
            event: "error",
            data: { code: "malformed_event", message: `Could not parse event ${eventName}` },
          } as DeliveredEvent;
          continue;
        }
        yield {
          id: seq,
          session_id: envelope.session_id,
          event: eventName,
          data: envelope.payload,
        } as DeliveredEvent;
      }
    },

    send: (sid, content) => input(sid, { type: "user_message", content }),
    interrupt: (sid) => input(sid, { type: "interrupt" }),
    approve(sid, correlationId, options = {}) {
      const msg: InboundMessage = {
        type: "permission_response",
        correlation_id: correlationId,
        behavior: "allow",
      };
      if (options.updatedInput !== undefined) msg.updated_input = options.updatedInput;
      if (options.updatedPermissions !== undefined) msg.updated_permissions = options.updatedPermissions;
      return input(sid, msg);
    },
    deny(sid, correlationId, options = {}) {
      const msg: InboundMessage = {
        type: "permission_response",
        correlation_id: correlationId,
        behavior: "deny",
      };
      if (options.message !== undefined) msg.message = options.message;
      if (options.interrupt !== undefined) msg.interrupt = options.interrupt;
      return input(sid, msg);
    },
    answer: (sid, correlationId, answers) =>
      input(sid, { type: "question_response", correlation_id: correlationId, answers }),
    setPermissionMode: (sid, mode) => input(sid, { type: "set_permission_mode", mode }),
    setModel: (sid, model) => input(sid, { type: "set_model", model }),
    stopTask: (sid, taskId) => input(sid, { type: "stop_task", task_id: taskId }),
  };
}
