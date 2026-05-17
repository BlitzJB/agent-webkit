import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  createAgentClient,
  TransportError,
  type AgentClient,
  type ApproveOptions,
  type DenyOptions,
  type DeliveredEvent,
  type SessionListEntry,
  type UserInput,
} from "@agent-webkit/core";
import {
  initialMuxState,
  initialSessionState,
  reduce,
  type Action,
  type MuxState,
  type SessionState,
} from "./reducer.js";
import { subscribeToStream } from "./streamSubscriber.js";

// ────────────────────────────────────────────────────────────────────────────
// L2 hook — multiplexed.
//
// One connection to /stream, ever. Per-session state lives in a keyed map.
// Components select a single session via `useActiveSession(mux, sid)` and
// get the same shape that the old `useAgentSession` returned.
// ────────────────────────────────────────────────────────────────────────────

export interface UseAgentMuxOptions {
  baseUrl: string;
  token?: string;
  /** Auto-connect /stream on mount. Defaults true. */
  autoStart?: boolean;
  /** Inject a client (tests / SSR). */
  client?: AgentClient;
  /** Optional event tap — called for every wire event before reducer dispatch. */
  onEvent?: (event: DeliveredEvent) => void;
}

export interface AgentMux extends MuxState {
  /** Client passthroughs — every action takes the session id. */
  send: (sessionId: string, input: UserInput) => Promise<void>;
  interrupt: (sessionId: string) => Promise<void>;
  approve: (sessionId: string, correlationId: string, options?: ApproveOptions) => Promise<void>;
  deny: (sessionId: string, correlationId: string, options?: DenyOptions) => Promise<void>;
  answer: (sessionId: string, correlationId: string, answers: unknown) => Promise<void>;
  setPermissionMode: (sessionId: string, mode: string) => Promise<void>;
  setModel: (sessionId: string, model: string | null) => Promise<void>;
  stopTask: (sessionId: string, taskId: string) => Promise<void>;

  /** Server-side session list — re-fetched on demand. */
  refreshSessions: () => Promise<SessionListEntry[]>;
  /** Create a new session (forwards to L1) and pre-seed the mux state slot. */
  createSession: (opts?: Parameters<AgentClient["createSession"]>[0]) => Promise<string>;
  /** Delete a session server-side AND drop its slot from local state. */
  deleteSession: (sessionId: string) => Promise<void>;
  /** Fetch past wire events for this session and replay into reducer state. */
  loadHistory: (sessionId: string) => Promise<void>;
  /** Clear the transient notice for a session (set by 409-on-reply etc.). */
  clearNotice: (sessionId: string) => void;

  /** Last known list of server-persisted sessions. */
  sessionList: SessionListEntry[];
  /** Did the initial /sessions fetch + /stream connect complete? */
  hydrated: boolean;
  /** Underlying L1 client — exposed for advanced cases. */
  client: AgentClient;
}

export function useAgentMux(opts: UseAgentMuxOptions): AgentMux {
  const [state, dispatch] = useReducer(reduce as (s: MuxState, a: Action) => MuxState, initialMuxState);
  const [sessionList, setSessionList] = useState<SessionListEntry[]>([]);
  const [hydrated, setHydrated] = useState(false);

  const { baseUrl, token, autoStart = true, client: injectedClient } = opts;
  const onEventRef = useRef(opts.onEvent);
  onEventRef.current = opts.onEvent;

  // Stable client per (baseUrl, token, injectedClient).
  const client = useMemo<AgentClient>(() => {
    if (injectedClient) return injectedClient;
    const clientOpts: { baseUrl: string; token?: string } = { baseUrl };
    if (token !== undefined) clientOpts.token = token;
    return createAgentClient(clientOpts);
  }, [baseUrl, token, injectedClient]);

  // Persistent /stream subscription via module-level singleton. One real
  // client.events() loop per AgentClient regardless of how many hook
  // instances mount — survives StrictMode double-mount, HMR remounts and
  // page-error reloads, so we never accumulate browser-side SSE sockets
  // past the HTTP/1.1 6-per-origin cap.
  useEffect(() => {
    if (!autoStart) return;
    const unsubscribe = subscribeToStream(client, {
      onEvent: (ev) => {
        const tap = onEventRef.current;
        if (tap) {
          try { tap(ev); } catch { /* taps must not break the reducer */ }
        }
        dispatch({ type: "server_event", event: ev });
      },
      onError: (err) => {
        dispatch({ type: "stream_error", error: err });
      },
    });
    return unsubscribe;
  }, [client, autoStart]);

  const refreshSessions = useCallback(async (): Promise<SessionListEntry[]> => {
    try {
      const { sessions } = await client.listSessions();
      // Newest activity first.
      const sorted = [...sessions].sort((a, b) => b.last_seen_at - a.last_seen_at);
      setSessionList(sorted);
      for (const s of sorted) {
        dispatch({ type: "ensure_session", sessionId: s.id });
      }
      return sorted;
    } catch {
      return [];
    }
  }, [client]);

  // Hydrate on mount.
  useEffect(() => {
    (async () => {
      await refreshSessions();
      setHydrated(true);
    })();
  }, [refreshSessions]);

  const createSession = useCallback(
    async (createOpts?: Parameters<AgentClient["createSession"]>[0]): Promise<string> => {
      const { session_id } = await client.createSession(createOpts);
      dispatch({ type: "ensure_session", sessionId: session_id });
      await refreshSessions();
      return session_id;
    },
    [client, refreshSessions]
  );

  const deleteSession = useCallback(
    async (sessionId: string): Promise<void> => {
      try {
        await client.deleteSession(sessionId);
      } catch {
        /* idempotent — proceed */
      }
      dispatch({ type: "remove_session", sessionId });
      await refreshSessions();
    },
    [client, refreshSessions]
  );

  const loadHistory = useCallback(
    async (sessionId: string): Promise<void> => {
      try {
        const { events } = await client.history(sessionId);
        dispatch({ type: "history_loaded", sessionId, events });
      } catch {
        /* swallow — UI can degrade to just live events */
      }
    },
    [client]
  );

  const send = useCallback(
    async (sessionId: string, input: UserInput): Promise<void> => {
      const localId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      dispatch({ type: "local_user_message", sessionId, content: input, localId });
      await client.send(sessionId, input);
    },
    [client]
  );
  const interrupt = useCallback((sid: string) => client.interrupt(sid), [client]);
  // A 409 from /input on permission/question responses means "first-reply
  // already won" — typically because we (or another tab) already answered
  // this correlation_id. Treat it as benign success and still dispatch the
  // resolved action so the UI clears, instead of leaving the modal stuck
  // with an opaque "Conflict" error.
  const isAlreadyAnswered = (err: unknown): boolean =>
    err instanceof TransportError && err.status === 409;

  const noticeForConflict = (
    sid: string,
    kind: "permission" | "question",
  ): void => {
    dispatch({
      type: "set_notice",
      sessionId: sid,
      code: "already_answered",
      message:
        kind === "permission"
          ? "Another subscriber already answered this permission request."
          : "Another subscriber already answered this question.",
    });
  };

  const approve = useCallback(
    async (sid: string, correlationId: string, options?: ApproveOptions): Promise<void> => {
      try {
        await client.approve(sid, correlationId, options);
      } catch (err) {
        if (!isAlreadyAnswered(err)) throw err;
        noticeForConflict(sid, "permission");
      }
      dispatch({ type: "permission_resolved", sessionId: sid, correlationId });
    },
    [client]
  );
  const deny = useCallback(
    async (sid: string, correlationId: string, options?: DenyOptions): Promise<void> => {
      try {
        await client.deny(sid, correlationId, options);
      } catch (err) {
        if (!isAlreadyAnswered(err)) throw err;
        noticeForConflict(sid, "permission");
      }
      dispatch({ type: "permission_resolved", sessionId: sid, correlationId });
    },
    [client]
  );
  const answer = useCallback(
    async (sid: string, correlationId: string, answers: unknown): Promise<void> => {
      try {
        await client.answer(sid, correlationId, answers);
      } catch (err) {
        if (!isAlreadyAnswered(err)) throw err;
        noticeForConflict(sid, "question");
      }
      dispatch({ type: "question_resolved", sessionId: sid, correlationId });
    },
    [client]
  );
  const clearNotice = useCallback((sid: string) => {
    dispatch({ type: "clear_notice", sessionId: sid });
  }, []);
  const setPermissionMode = useCallback(
    (sid: string, mode: string) => client.setPermissionMode(sid, mode),
    [client]
  );
  const setModel = useCallback(
    (sid: string, model: string | null) => client.setModel(sid, model),
    [client]
  );
  const stopTask = useCallback(
    (sid: string, taskId: string) => client.stopTask(sid, taskId),
    [client]
  );

  return {
    ...state,
    sessionList,
    hydrated,
    client,
    send,
    interrupt,
    approve,
    deny,
    answer,
    setPermissionMode,
    setModel,
    stopTask,
    refreshSessions,
    createSession,
    deleteSession,
    loadHistory,
    clearNotice,
  };
}

/**
 * Selector hook — wrap a mux instance + session id into the shape components
 * already expect (mirrors the pre-multiplex useAgentSession return).
 */
export function useActiveSession(mux: AgentMux, sessionId: string | null) {
  const session: SessionState = sessionId && mux.sessions[sessionId] ? mux.sessions[sessionId]! : initialSessionState;
  return {
    ...session,
    sessionId,
    // Curry the mux actions with this sessionId so the call site stays terse.
    send: (input: UserInput) =>
      sessionId ? mux.send(sessionId, input) : Promise.reject(new Error("no active session")),
    interrupt: () =>
      sessionId ? mux.interrupt(sessionId) : Promise.reject(new Error("no active session")),
    approve: (correlationId: string, options?: ApproveOptions) =>
      sessionId ? mux.approve(sessionId, correlationId, options) : Promise.reject(new Error("no active session")),
    deny: (correlationId: string, options?: DenyOptions) =>
      sessionId ? mux.deny(sessionId, correlationId, options) : Promise.reject(new Error("no active session")),
    answer: (correlationId: string, answers: unknown) =>
      sessionId ? mux.answer(sessionId, correlationId, answers) : Promise.reject(new Error("no active session")),
    setPermissionMode: (mode: string) =>
      sessionId ? mux.setPermissionMode(sessionId, mode) : Promise.reject(new Error("no active session")),
    setModel: (model: string | null) =>
      sessionId ? mux.setModel(sessionId, model) : Promise.reject(new Error("no active session")),
    stopTask: (taskId: string) =>
      sessionId ? mux.stopTask(sessionId, taskId) : Promise.reject(new Error("no active session")),
  };
}
