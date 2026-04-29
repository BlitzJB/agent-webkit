/**
 * Partial-JSON parser specialized for streamed tool inputs.
 *
 * Given a possibly-truncated JSON document (a prefix of a future-complete document),
 * return the most informative `{ value, complete }` we can produce, or `null` if no
 * coherent state is parseable yet.
 *
 * Behaviour:
 *  - In-flight strings are NOT surfaced (we wait for the closing `"`). This prevents
 *    half-typed flickers in skeleton renderers.
 *  - In-flight numbers / literals (`tru`, `fal`, `12.`) are NOT surfaced for the same
 *    reason — only fully tokenised primitives count.
 *  - `complete: true` means the input was already valid JSON without any patching.
 *  - `complete: false` means we synthesised closing brackets to make it parseable.
 *
 * Implementation strategy: scan once to collect "safe checkpoints" (positions at which
 * the document, suffixed by appropriate closing brackets, would parse). Try the latest
 * first, falling back to earlier ones on failure. ~120 LOC, no dependencies.
 */

export interface PartialJSONResult<T = unknown> {
  value: T;
  complete: boolean;
}

interface Checkpoint {
  end: number;
  closing: string;
  trimComma: boolean;
}

export function parsePartialJSON<T = unknown>(input: string): PartialJSONResult<T> | null {
  const trimmed = input.trimEnd();
  if (!trimmed) return null;

  // Fast path: input is already valid JSON.
  try {
    return { value: JSON.parse(trimmed) as T, complete: true };
  } catch {
    /* fall through */
  }

  const stack: Array<"}" | "]"> = [];
  // Track whether we are positioned where an object key (vs value) is expected.
  // Top-of-stack-relevant only inside objects; arrays don't have keys.
  // expectKey[depth] === true means we just saw `{` or `,` inside an object.
  const expectKey: boolean[] = [];
  const checkpoints: Checkpoint[] = [];

  const pushCheckpoint = (end: number, trimComma = false) => {
    checkpoints.push({
      end,
      closing: stack.slice().reverse().join(""),
      trimComma,
    });
  };

  let i = 0;
  let parsedRoot = false;
  scan: while (i < input.length) {
    const c = input[i] as string;
    // Whitespace.
    if (c === " " || c === "\t" || c === "\n" || c === "\r") {
      i++;
      continue;
    }
    if (c === "{") {
      stack.push("}");
      expectKey.push(true);
      i++;
      continue;
    }
    if (c === "[") {
      stack.push("]");
      expectKey.push(false);
      i++;
      continue;
    }
    if (c === "}" || c === "]") {
      const expected = stack.pop();
      expectKey.pop();
      if (expected !== c) {
        // Malformed. Stop scanning; we may still have earlier checkpoints.
        break scan;
      }
      i++;
      if (stack.length > 0) {
        // Top of stack is an object/array that contains us; we just produced a value
        // from its perspective.
        if (stack[stack.length - 1] === "}") {
          expectKey[expectKey.length - 1] = false; // still inside an object — value just emitted, expect comma or }
        }
      } else {
        parsedRoot = true;
      }
      pushCheckpoint(i);
      if (stack.length === 0) break scan; // root closed
      continue;
    }
    if (c === ",") {
      // After a value (or a key:value pair). Truncating here (excluding the comma)
      // is safe.
      pushCheckpoint(i, true);
      // Expect a key next if we're inside an object.
      if (stack.length > 0 && stack[stack.length - 1] === "}") {
        expectKey[expectKey.length - 1] = true;
      }
      i++;
      continue;
    }
    if (c === ":") {
      // After an object key — value expected next.
      if (stack.length > 0 && stack[stack.length - 1] === "}") {
        expectKey[expectKey.length - 1] = false;
      }
      i++;
      continue;
    }
    if (c === '"') {
      // Walk to the closing quote, respecting escapes.
      const start = i;
      i++;
      let closed = false;
      while (i < input.length) {
        const ch = input[i];
        if (ch === "\\") {
          i += 2;
          continue;
        }
        if (ch === '"') {
          i++;
          closed = true;
          break;
        }
        i++;
      }
      if (!closed) {
        // Unfinished string — no checkpoint; suppress surfacing partials.
        break scan;
      }
      // We just finished a string. If we were expecting a key, we don't push
      // a checkpoint here (the value is still missing).
      const wasKey =
        stack.length > 0 &&
        stack[stack.length - 1] === "}" &&
        expectKey[expectKey.length - 1];
      if (!wasKey) {
        // It's a value (in an object after `:` or in an array or root).
        if (stack.length > 0 && stack[stack.length - 1] === "}") {
          expectKey[expectKey.length - 1] = false;
        }
        pushCheckpoint(i);
        if (stack.length === 0) {
          parsedRoot = true;
          break scan;
        }
      }
      // Validate the string itself parses cleanly (escape sequences could be invalid).
      try {
        JSON.parse(input.slice(start, i));
      } catch {
        // Roll back the last checkpoint we just added since the string is malformed.
        if (!wasKey && checkpoints.length > 0) checkpoints.pop();
        break scan;
      }
      continue;
    }
    // Literals: true, false, null, numbers.
    if (c === "t" || c === "f" || c === "n" || c === "-" || (c >= "0" && c <= "9")) {
      const start = i;
      while (i < input.length) {
        const ch = input[i];
        if (ch === undefined) break;
        if (!/[-0-9.eE+truefalsn]/.test(ch)) break;
        i++;
      }
      const tok = input.slice(start, i);
      // Only treat as a checkpoint if the literal fully parses as JSON AND we know
      // it's terminated (i.e. there's a delimiter immediately after). A number/literal
      // at the very end of the buffer is ambiguous: `7` could become `72`, `tru` could
      // become `true`, etc. We surface only when the next character closes the value.
      const nextCh = input[i];
      const terminated =
        nextCh !== undefined &&
        (nextCh === "," ||
          nextCh === "}" ||
          nextCh === "]" ||
          nextCh === " " ||
          nextCh === "\t" ||
          nextCh === "\n" ||
          nextCh === "\r");
      let valid = false;
      try {
        JSON.parse(tok);
        valid = true;
      } catch {
        /* in-flight literal — bail */
      }
      if (!valid) break scan;
      if (stack.length > 0 && stack[stack.length - 1] === "}") {
        expectKey[expectKey.length - 1] = false;
      }
      if (terminated) {
        pushCheckpoint(i);
      } else if (stack.length === 0) {
        // Root-level literal at EOF — surface it (e.g. `true`, `42`).
        pushCheckpoint(i);
        parsedRoot = true;
        break scan;
      }
      continue;
    }
    // Unknown character — bail.
    break scan;
  }

  // Try checkpoints in reverse order (latest first).
  for (let k = checkpoints.length - 1; k >= 0; k--) {
    const cp = checkpoints[k];
    if (!cp) continue;
    let candidate = input.slice(0, cp.end);
    if (cp.trimComma && candidate.endsWith(",")) {
      candidate = candidate.slice(0, -1);
    }
    try {
      return { value: JSON.parse(candidate + cp.closing) as T, complete: false };
    } catch {
      /* try earlier */
    }
  }

  // Final fallback for empty containers like `{` or `{ "k":` — emit empty object/array.
  const head = input.trimStart()[0];
  if (head === "{") return { value: {} as T, complete: false };
  if (head === "[") return { value: [] as T, complete: false };
  return null;
}
