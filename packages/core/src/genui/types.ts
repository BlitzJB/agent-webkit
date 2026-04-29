/** Wire shape of `GET /genui/schema` (server-emitted). */
export interface GenUISchemaToolEntry {
  /** Fully-qualified tool name as the SDK emits it (e.g. `"mcp__genui__render_weather_card"`). */
  name: string;
  /** User-facing short name (e.g. `"weather_card"`). The L2 hook keys renderers by this. */
  short_name: string;
  /** The MCP tool's own name without server prefix (e.g. `"render_weather_card"`). */
  raw_tool_name: string;
  description: string;
  /** JSON Schema describing the props. */
  schema: Record<string, unknown>;
}

export interface GenUISchemaPayload {
  version: string;
  server_name: string;
  prefix: string;
  tools: GenUISchemaToolEntry[];
}

/** One emitted update produced by `GenUIStream.feed()`. */
export interface GenUIUpdate<P = Record<string, unknown>> {
  /** Stable id of the originating `tool_use` (per-message). */
  toolUseId: string;
  /** Id of the assistant message that emitted this tool_use. */
  messageId: string;
  /** User-facing short name; matches keys in renderer registries. */
  shortName: string;
  /** Fully-qualified wire name. */
  qualifiedName: string;
  /** Best-effort props parsed so far. May be a subset of the final shape when `partial`. */
  props: Partial<P>;
  /** True while still streaming and the input has not yet been fully validated. */
  partial: boolean;
  /** True when the final tool_use input has been received and parsed. */
  complete: boolean;
  /** Optional schema-validation error on the final parse. None today; reserved. */
  error?: { message: string } | null;
}
