import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { DeliveredEvent } from "@agent-webkit/core";
import {
  GenUIStream,
  type GenUISchemaPayload,
  type GenUIStreamOptions,
  type GenUIUpdate,
} from "@agent-webkit/core/genui";

export type GenUIRenderer<P = Record<string, unknown>> = (
  props: P,
  meta: { update: GenUIUpdate<P>; complete: boolean; partial: boolean },
) => ReactNode;

export type GenUIRenderers = Record<string, GenUIRenderer>;

export interface UseGenerativeUIOptions extends GenUIStreamOptions {
  renderers: GenUIRenderers;
  /** Auto-load schema from `schemaUrl` on mount. Defaults to true when schemaUrl is set. */
  autoLoadSchema?: boolean;
}

export interface UseGenerativeUIReturn {
  /** The event tap to wire into `useAgentSession({ onEvent })`. */
  onEvent: (event: DeliveredEvent) => void;
  /** Latest GenUI updates, keyed by tool_use_id, in arrival order. */
  updates: GenUIUpdate[];
  /** Schema (after load), or null. */
  schema: GenUISchemaPayload | null;
  /** Render the update via its registered renderer, or null if unregistered. */
  render: (update: GenUIUpdate) => ReactNode;
  /** Reset internal state — call between sessions. */
  reset: () => void;
}

export function useGenerativeUI(opts: UseGenerativeUIOptions): UseGenerativeUIReturn {
  const { renderers, autoLoadSchema, ...streamOpts } = opts;

  const streamRef = useRef<GenUIStream | null>(null);
  if (!streamRef.current) {
    streamRef.current = new GenUIStream(streamOpts);
  }
  const [schema, setSchema] = useState<GenUISchemaPayload | null>(streamOpts.schema ?? null);
  const [updates, setUpdates] = useState<GenUIUpdate[]>([]);
  const indexRef = useRef<Map<string, number>>(new Map());

  const renderersRef = useRef(renderers);
  renderersRef.current = renderers;

  useEffect(() => {
    const shouldLoad = autoLoadSchema ?? Boolean(streamOpts.schemaUrl);
    if (!shouldLoad) return;
    let cancelled = false;
    streamRef.current!
      .loadSchema()
      .then((s) => {
        if (!cancelled) setSchema(s);
      })
      .catch(() => {
        /* surfaced via render-time mismatch; tests assert separately */
      });
    return () => {
      cancelled = true;
    };
  }, [autoLoadSchema, streamOpts.schemaUrl]);

  const onEvent = useCallback((event: DeliveredEvent) => {
    const update = streamRef.current!.feed(event);
    if (!update) return;
    setUpdates((prev) => {
      const idx = indexRef.current.get(update.toolUseId);
      if (idx === undefined) {
        indexRef.current.set(update.toolUseId, prev.length);
        return [...prev, update];
      }
      const next = prev.slice();
      next[idx] = update;
      return next;
    });
  }, []);

  const render = useCallback((update: GenUIUpdate): ReactNode => {
    const r = renderersRef.current[update.shortName];
    if (!r) return null;
    return r(update.props, {
      update,
      complete: update.complete,
      partial: update.partial,
    });
  }, []);

  const reset = useCallback(() => {
    streamRef.current!.reset();
    indexRef.current.clear();
    setUpdates([]);
  }, []);

  return useMemo(
    () => ({ onEvent, updates, schema, render, reset }),
    [onEvent, updates, schema, render, reset],
  );
}
