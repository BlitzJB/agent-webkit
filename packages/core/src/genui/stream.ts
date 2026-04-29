/**
 * GenUIStream — framework-free state machine that turns wire events into
 * `GenUIUpdate` records.
 *
 * Pipeline:
 *   wire `tool_use` event       → match qualified name → emit complete update
 *   wire `message_delta` event  → if `delta.type === "input_json_delta"`, buffer
 *                                  partial JSON per tool_use_id and emit partial
 *                                  updates as the buffer parses cleanly
 *
 * No React, no DOM. Pass it a parsed wire event; receive an update or `null`.
 *
 * The matching strategy: events with `tool_name` starting with
 * `mcp__<server>__<prefix>` AND ending with a registered short name are
 * dispatched. Anything else is ignored (returned as `null`).
 */

import type { DeliveredEvent } from "../types.js";
import { parsePartialJSON } from "./partial-json.js";
import type { GenUISchemaPayload, GenUISchemaToolEntry, GenUIUpdate } from "./types.js";

export interface GenUIStreamOptions {
  /** Pre-fetched schema payload. Use this OR `schemaUrl`. */
  schema?: GenUISchemaPayload;
  /** URL of `GET /genui/schema`. Resolved by `loadSchema()`. */
  schemaUrl?: string;
  /** Optional fetch implementation (defaults to global `fetch`). */
  fetchImpl?: typeof fetch;
  /** Optional auth headers for the schema fetch. */
  headers?: Record<string, string>;
  /**
   * If neither schema nor schemaUrl is provided, `feed()` will dispatch any
   * tool_use whose name starts with `mcp__<serverName>__<prefix>`. Defaults:
   * `serverName="genui"`, `prefix="render_"`.
   */
  serverName?: string;
  prefix?: string;
}

interface PendingDelta {
  buffer: string;
  messageId: string;
  qualifiedName: string;
  shortName: string;
}

export class GenUIStream {
  private schema: GenUISchemaPayload | null;
  private schemaUrl: string | undefined;
  private readonly fetchImpl: typeof fetch | undefined;
  private readonly headers: Record<string, string>;
  private readonly serverName: string;
  private readonly prefix: string;
  private readonly pendingDeltas = new Map<string, PendingDelta>();
  private readonly settled = new Set<string>();

  constructor(opts: GenUIStreamOptions = {}) {
    this.schema = opts.schema ?? null;
    this.schemaUrl = opts.schemaUrl;
    this.fetchImpl = opts.fetchImpl;
    this.headers = opts.headers ?? {};
    this.serverName = opts.serverName ?? this.schema?.server_name ?? "genui";
    this.prefix = opts.prefix ?? this.schema?.prefix ?? "render_";
  }

  /** Fetch the schema from `schemaUrl` if it wasn't supplied at construction. */
  async loadSchema(): Promise<GenUISchemaPayload | null> {
    if (this.schema) return this.schema;
    if (!this.schemaUrl) return null;
    const f = this.fetchImpl ?? globalThis.fetch;
    if (!f) throw new Error("GenUIStream: no fetch implementation available");
    const res = await f(this.schemaUrl, { headers: this.headers });
    if (!res.ok) {
      throw new Error(`GenUIStream: GET ${this.schemaUrl} → ${res.status}`);
    }
    this.schema = (await res.json()) as GenUISchemaPayload;
    return this.schema;
  }

  /** All known tool entries, derived from the schema (or empty if none loaded). */
  entries(): readonly GenUISchemaToolEntry[] {
    return this.schema?.tools ?? [];
  }

  /**
   * Drive the stream forward by one wire event. Returns an update if this event
   * concerns a registered GenUI tool, or `null` otherwise.
   */
  feed(event: DeliveredEvent): GenUIUpdate | null {
    if (event.event === "tool_use") {
      const data = event.data;
      const match = this.matchToolName(data.tool_name);
      if (!match) return null;
      // Final, complete tool input. Drop any buffered deltas for this id.
      this.pendingDeltas.delete(data.tool_use_id);
      this.settled.add(data.tool_use_id);
      return {
        toolUseId: data.tool_use_id,
        messageId: data.message_id,
        shortName: match.short,
        qualifiedName: data.tool_name,
        props: (data.input ?? {}) as Record<string, unknown>,
        partial: false,
        complete: true,
        error: null,
      };
    }

    if (event.event === "message_delta") {
      const data = event.data as {
        message_id: string;
        delta: { type?: string; id?: string; name?: string; partial_json?: string; tool_use_id?: string };
      };
      const delta = data.delta as Record<string, unknown> | undefined;
      if (!delta || delta.type !== "input_json_delta") return null;
      const toolUseId = (delta.tool_use_id ?? delta.id) as string | undefined;
      if (!toolUseId) return null;
      if (this.settled.has(toolUseId)) return null;

      let pending = this.pendingDeltas.get(toolUseId);
      if (!pending) {
        // First delta for this tool — try to learn the qualified name from the delta
        // itself (some SDK shapes include `name`), or skip until we get one.
        const name = (delta.name as string | undefined) ?? "";
        const match = this.matchToolName(name);
        if (!match) {
          // No name yet — buffer eagerly and try to attach later.
          pending = {
            buffer: "",
            messageId: data.message_id,
            qualifiedName: "",
            shortName: "",
          };
        } else {
          pending = {
            buffer: "",
            messageId: data.message_id,
            qualifiedName: name,
            shortName: match.short,
          };
        }
        this.pendingDeltas.set(toolUseId, pending);
      }
      // Append the new partial JSON chunk.
      const chunk = (delta.partial_json as string | undefined) ?? "";
      pending.buffer += chunk;

      // Late-binding: if we still don't know the qualified name, drop the update
      // (we'll reconcile when the final tool_use event arrives with the full name).
      if (!pending.qualifiedName) return null;

      const parsed = parsePartialJSON<Record<string, unknown>>(pending.buffer);
      if (!parsed) return null;
      return {
        toolUseId,
        messageId: pending.messageId,
        shortName: pending.shortName,
        qualifiedName: pending.qualifiedName,
        props: parsed.value,
        partial: !parsed.complete,
        complete: false,
        error: null,
      };
    }
    return null;
  }

  /** Reset internal state. Call this when the user starts a fresh session. */
  reset(): void {
    this.pendingDeltas.clear();
    this.settled.clear();
  }

  private matchToolName(name: string): { short: string } | null {
    if (!name) return null;
    if (this.schema) {
      const entry = this.schema.tools.find((t) => t.name === name);
      if (entry) return { short: entry.short_name };
      return null;
    }
    // No schema loaded: fall back to prefix matching.
    const head = `mcp__${this.serverName}__${this.prefix}`;
    if (!name.startsWith(head)) return null;
    return { short: name.slice(head.length) };
  }
}
