import type {
  ApproveOptions,
  CreateSessionOptions,
  CreateSessionResponse,
  DeliveredEvent,
  DenyOptions,
  InboundMessage,
  ServerEvent,
  UserInput,
} from "./types.js";
import { Transport, TransportError } from "./transport.js";

export interface SessionHandle {
  readonly id: string;
  readonly protocolVersion: string;
  /**
   * Async-iterable of typed, ordered events. Includes resumed events on reconnect
   * (deduplicated via server seq id).
   */
  events(): AsyncIterable<DeliveredEvent>;
  send(input: UserInput): Promise<void>;
  interrupt(): Promise<void>;
  approve(correlationId: string, options?: ApproveOptions): Promise<void>;
  deny(correlationId: string, options?: DenyOptions): Promise<void>;
  answer(correlationId: string, answers: unknown): Promise<void>;
  setPermissionMode(mode: string): Promise<void>;
  setModel(model: string | null): Promise<void>;
  stopTask(taskId: string): Promise<void>;
  /**
   * Abort the SSE stream locally. Does NOT delete the server-side session, so a future
   * `attachSession()` (or remount) can resume from `lastEventId`.
   */
  detach(): void;
  /**
   * Tear down the server-side session via DELETE. Idempotent — also aborts the local stream.
   * Use this for permanent shutdown, not for component unmount in StrictMode.
   */
  close(): Promise<void>;
  /** Last event seq seen — useful for caller-side resume. */
  readonly lastEventId: string | undefined;
}

export class Session implements SessionHandle {
  readonly id: string;
  readonly protocolVersion: string;
  private readonly transport: Transport;
  private readonly abort: AbortController;
  private _lastEventId: string | undefined;

  constructor(
    transport: Transport,
    info: CreateSessionResponse,
    opts?: { resumeFromEventId?: string }
  ) {
    this.transport = transport;
    this.id = info.session_id;
    this.protocolVersion = info.protocol_version;
    this.abort = new AbortController();
    this._lastEventId = opts?.resumeFromEventId;
  }

  get lastEventId(): string | undefined {
    return this._lastEventId;
  }

  async *events(): AsyncIterable<DeliveredEvent> {
    const path = `/sessions/${encodeURIComponent(this.id)}/stream`;
    const streamOpts: { signal: AbortSignal; lastEventId?: string } = { signal: this.abort.signal };
    if (this._lastEventId !== undefined) streamOpts.lastEventId = this._lastEventId;

    for await (const raw of this.transport.streamEvents(path, streamOpts)) {
      if (raw.id !== undefined) this._lastEventId = raw.id;
      const eventName = raw.event ?? "message";
      let parsed: unknown;
      try {
        parsed = raw.data === "" ? {} : JSON.parse(raw.data);
      } catch {
        // Malformed payload — surface as an error event rather than throwing,
        // so consumer's iteration is not silently broken.
        const seq = raw.id !== undefined ? Number(raw.id) : -1;
        yield {
          id: seq,
          event: "error",
          data: { code: "malformed_event", message: `Could not parse event ${eventName}` },
        };
        continue;
      }
      const seq = raw.id !== undefined ? Number(raw.id) : -1;
      // We trust the server-side schema here (validated by Pydantic on the server,
      // shared types in TS). Cast to the discriminated union.
      yield { id: seq, event: eventName, data: parsed } as DeliveredEvent;
      if (eventName === "done") return;
    }
  }

  private async input(msg: InboundMessage): Promise<void> {
    const res = await this.transport.post(`/sessions/${encodeURIComponent(this.id)}/input`, msg);
    if (res.status === 204) return;
    if (res.status === 409) {
      const text = await res.text().catch(() => "");
      throw new TransportError(`Conflict: another subscriber already replied`, 409, text);
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new TransportError(`Input rejected: ${res.status}`, res.status, text);
    }
  }

  send(input: UserInput): Promise<void> {
    return this.input({ type: "user_message", content: input });
  }
  interrupt(): Promise<void> {
    return this.input({ type: "interrupt" });
  }
  approve(correlationId: string, options: ApproveOptions = {}): Promise<void> {
    const msg: InboundMessage = {
      type: "permission_response",
      correlation_id: correlationId,
      behavior: "allow",
    };
    if (options.updatedInput !== undefined) msg.updated_input = options.updatedInput;
    if (options.updatedPermissions !== undefined) msg.updated_permissions = options.updatedPermissions;
    return this.input(msg);
  }
  deny(correlationId: string, options: DenyOptions = {}): Promise<void> {
    const msg: InboundMessage = {
      type: "permission_response",
      correlation_id: correlationId,
      behavior: "deny",
    };
    if (options.message !== undefined) msg.message = options.message;
    if (options.interrupt !== undefined) msg.interrupt = options.interrupt;
    return this.input(msg);
  }
  answer(correlationId: string, answers: unknown): Promise<void> {
    return this.input({ type: "question_response", correlation_id: correlationId, answers });
  }
  setPermissionMode(mode: string): Promise<void> {
    return this.input({ type: "set_permission_mode", mode });
  }
  setModel(model: string | null): Promise<void> {
    return this.input({ type: "set_model", model });
  }
  stopTask(taskId: string): Promise<void> {
    return this.input({ type: "stop_task", task_id: taskId });
  }

  detach(): void {
    this.abort.abort();
  }

  async close(): Promise<void> {
    this.abort.abort();
    try {
      await this.transport.delete(`/sessions/${encodeURIComponent(this.id)}`);
    } catch (err) {
      // best-effort
    }
  }
}

export interface AgentClient {
  createSession(opts?: CreateSessionOptions): Promise<Session>;
  attachSession(sessionId: string, opts?: { resumeFromEventId?: string }): Session;
}

export interface CreateAgentClientOptions {
  baseUrl: string;
  token?: string | undefined;
  fetchImpl?: typeof fetch | undefined;
}

export function createAgentClient(opts: CreateAgentClientOptions): AgentClient {
  const transport = new Transport(opts);
  return {
    async createSession(createOpts) {
      const info = await transport.postJSON<CreateSessionResponse>("/sessions", createOpts ?? {});
      return new Session(transport, info);
    },
    attachSession(sessionId, attachOpts) {
      const info: CreateSessionResponse = { session_id: sessionId, protocol_version: "1.0" };
      return new Session(transport, info, attachOpts);
    },
  };
}
