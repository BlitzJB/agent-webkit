import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  createAgentClient,
  type AgentClient,
  type ApproveOptions,
  type CreateSessionOptions,
  type DenyOptions,
  type Session,
  type UserInput,
} from "@agent-webkit/core";
import {
  initialState,
  reduce,
  type AgentState,
  type Action,
} from "./reducer.js";

export interface UseAgentSessionOptions {
  baseUrl: string;
  token?: string;
  /** If provided, attach to an existing session instead of creating a new one. */
  sessionId?: string;
  /** Resume from a specific event seq id (used when reconnecting). */
  resumeFromEventId?: string;
  /** Options forwarded to POST /sessions when creating a new session. */
  create?: CreateSessionOptions;
  /** Inject a client (useful for tests). */
  client?: AgentClient;
  /** Auto-start the session on mount. Defaults to true. */
  autoStart?: boolean;
}

export interface UseAgentSessionReturn extends AgentState {
  sessionId: string | null;
  send: (input: UserInput) => Promise<void>;
  interrupt: () => Promise<void>;
  approve: (correlationId: string, options?: ApproveOptions) => Promise<void>;
  deny: (correlationId: string, options?: DenyOptions) => Promise<void>;
  answer: (correlationId: string, answers: unknown) => Promise<void>;
  setPermissionMode: (mode: string) => Promise<void>;
  setModel: (model: string | null) => Promise<void>;
  stopTask: (taskId: string) => Promise<void>;
  close: () => Promise<void>;
}

/**
 * useAgentSession — connects to an agent-webkit server, streams events into a
 * reconciled message list, and exposes typed actions.
 *
 * The hook only creates one session per mount cycle; changing baseUrl/sessionId/token
 * across renders triggers a re-create.
 */
export function useAgentSession(opts: UseAgentSessionOptions): UseAgentSessionReturn {
  const [state, dispatch] = useReducer(reduce as (s: AgentState, a: Action) => AgentState, initialState);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const sessionRef = useRef<Session | null>(null);
  const closedRef = useRef(false);

  // We deliberately re-key on baseUrl/sessionId/token, not the whole opts object.
  const { baseUrl, token, sessionId: attachSessionId, resumeFromEventId, autoStart = true } = opts;
  const createOpts = opts.create;
  const injectedClient = opts.client;

  useEffect(() => {
    if (!autoStart) return;
    closedRef.current = false;

    const clientOpts: { baseUrl: string; token?: string } = { baseUrl };
    if (token !== undefined) clientOpts.token = token;
    const client = injectedClient ?? createAgentClient(clientOpts);

    let aborted = false;

    (async () => {
      let session: Session;
      if (attachSessionId) {
        const attachOpts = resumeFromEventId !== undefined ? { resumeFromEventId } : {};
        session = client.attachSession(attachSessionId, attachOpts) as Session;
      } else {
        session = await client.createSession(createOpts);
      }
      if (aborted) {
        // StrictMode raced us — abort the local stream but leave the server session alive.
        // Cleanup happens via reaper / explicit close().
        session.detach();
        return;
      }
      sessionRef.current = session;
      setSessionId(session.id);

      try {
        for await (const ev of session.events()) {
          if (aborted) break;
          dispatch({ type: "server_event", event: ev });
        }
      } catch (err) {
        if (!aborted) {
          dispatch({
            type: "server_event",
            event: {
              id: -1,
              event: "error",
              data: {
                code: "stream_error",
                message: err instanceof Error ? err.message : String(err),
              },
            },
          });
        }
      }
    })();

    return () => {
      aborted = true;
      closedRef.current = true;
      const s = sessionRef.current;
      if (s) {
        // Detach only — do NOT delete the server-side session here. StrictMode double-
        // invokes effects, and remounts/HMR would otherwise destroy a session that the
        // user expects to outlive the component lifecycle. Permanent teardown is the
        // caller's responsibility via `close()`.
        s.detach();
      }
      sessionRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, token, attachSessionId, autoStart, injectedClient]);

  const requireSession = (): Session => {
    const s = sessionRef.current;
    if (!s) throw new Error("Session not yet ready");
    return s;
  };

  const send = useCallback(async (input: UserInput) => {
    const localId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    dispatch({ type: "local_user_message", content: input, localId });
    await requireSession().send(input);
  }, []);

  const interrupt = useCallback(async () => {
    await requireSession().interrupt();
  }, []);

  const approve = useCallback(async (correlationId: string, options?: ApproveOptions) => {
    await requireSession().approve(correlationId, options);
    dispatch({ type: "permission_resolved", correlationId });
  }, []);

  const deny = useCallback(async (correlationId: string, options?: DenyOptions) => {
    await requireSession().deny(correlationId, options);
    dispatch({ type: "permission_resolved", correlationId });
  }, []);

  const answer = useCallback(async (correlationId: string, answers: unknown) => {
    await requireSession().answer(correlationId, answers);
    dispatch({ type: "question_resolved", correlationId });
  }, []);

  const setPermissionMode = useCallback(async (mode: string) => {
    await requireSession().setPermissionMode(mode);
  }, []);
  const setModel = useCallback(async (model: string | null) => {
    await requireSession().setModel(model);
  }, []);
  const stopTask = useCallback(async (taskId: string) => {
    await requireSession().stopTask(taskId);
  }, []);
  const close = useCallback(async () => {
    const s = sessionRef.current;
    sessionRef.current = null;
    if (s) await s.close();
  }, []);

  return {
    ...state,
    sessionId,
    send,
    interrupt,
    approve,
    deny,
    answer,
    setPermissionMode,
    setModel,
    stopTask,
    close,
  };
}
