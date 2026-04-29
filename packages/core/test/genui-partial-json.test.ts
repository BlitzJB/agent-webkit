import { describe, it, expect } from "vitest";
import { parsePartialJSON } from "../src/genui/partial-json.js";

describe("parsePartialJSON — fast paths", () => {
  it("parses empty inputs as null", () => {
    expect(parsePartialJSON("")).toBeNull();
    expect(parsePartialJSON("   ")).toBeNull();
  });

  it("parses already-valid JSON with complete:true", () => {
    expect(parsePartialJSON(`{"a":1}`)).toEqual({ value: { a: 1 }, complete: true });
    expect(parsePartialJSON(`[1,2,3]`)).toEqual({ value: [1, 2, 3], complete: true });
    expect(parsePartialJSON(`"hi"`)).toEqual({ value: "hi", complete: true });
    expect(parsePartialJSON(`null`)).toEqual({ value: null, complete: true });
    expect(parsePartialJSON(`true`)).toEqual({ value: true, complete: true });
  });
});

describe("parsePartialJSON — streamed object building", () => {
  it("returns empty object on bare opening brace", () => {
    expect(parsePartialJSON(`{`)).toEqual({ value: {}, complete: false });
  });

  it("does not surface a key without value", () => {
    // We don't surface partial values; a key alone shouldn't appear.
    const r = parsePartialJSON(`{"location"`);
    expect(r?.value).toEqual({});
  });

  it("surfaces first complete key/value", () => {
    const r = parsePartialJSON(`{"location":"Boston"`);
    expect(r).toEqual({ value: { location: "Boston" }, complete: false });
  });

  it("surfaces first key/value while second is in flight (string)", () => {
    const r = parsePartialJSON(`{"location":"Boston","temperature_f":7`);
    // 7 is in-flight (might be 72) → not surfaced.
    expect(r?.value).toEqual({ location: "Boston" });
  });

  it("surfaces first key/value while second is in flight (open string)", () => {
    const r = parsePartialJSON(`{"location":"Boston","condition":"sun`);
    expect(r?.value).toEqual({ location: "Boston" });
  });

  it("does NOT surface a number until a delimiter follows it (avoid 7→72 flicker)", () => {
    // Numbers are uniquely ambiguous in streamed JSON: `7` is a valid prefix of `72`.
    // We hold them back until a comma/brace tells us the value is final.
    const r = parsePartialJSON(`{"location":"Boston","temperature_f":72`);
    expect(r?.value).toEqual({ location: "Boston" });
  });

  it("surfaces a number once a delimiter follows", () => {
    const r = parsePartialJSON(`{"location":"Boston","temperature_f":72,`);
    expect(r?.value).toEqual({ location: "Boston", temperature_f: 72 });
  });

  it("handles trailing comma after a value", () => {
    const r = parsePartialJSON(`{"a":1,`);
    expect(r?.value).toEqual({ a: 1 });
    expect(r?.complete).toBe(false);
  });

  it("nested objects", () => {
    const r = parsePartialJSON(`{"meta":{"id":"abc"`);
    expect(r?.value).toEqual({ meta: { id: "abc" } });
  });

  it("nested arrays of objects", () => {
    const r = parsePartialJSON(`{"plans":[{"name":"basic","price":10,`);
    expect(r?.value).toEqual({ plans: [{ name: "basic", price: 10 }] });
  });

  it("monotonic progression simulating input_json_delta chunks", () => {
    const ticks = [
      `{`,
      `{"location"`,
      `{"location":"`,
      `{"location":"Boston"`,
      `{"location":"Boston",`,
      `{"location":"Boston","temperature_f"`,
      `{"location":"Boston","temperature_f":72`,
      `{"location":"Boston","temperature_f":72,`,
      `{"location":"Boston","temperature_f":72,"condition"`,
      `{"location":"Boston","temperature_f":72,"condition":"sunny"`,
      `{"location":"Boston","temperature_f":72,"condition":"sunny"}`,
    ];
    const observed = ticks.map((t) => parsePartialJSON(t));
    // Each tick should produce a monotonically growing or equal-size object.
    let lastSize = -1;
    for (const r of observed) {
      expect(r).not.toBeNull();
      const v = r!.value as Record<string, unknown>;
      const size = Object.keys(v).length;
      expect(size).toBeGreaterThanOrEqual(lastSize);
      lastSize = size;
    }
    // Final tick should be complete.
    expect(observed[observed.length - 1]?.complete).toBe(true);
    expect(observed[observed.length - 1]?.value).toEqual({
      location: "Boston",
      temperature_f: 72,
      condition: "sunny",
    });
  });

  it("does not surface in-flight literal `tru`", () => {
    const r = parsePartialJSON(`{"flag":tru`);
    expect(r?.value).toEqual({});
  });

  it("surfaces null literal completed once delimited", () => {
    const r = parsePartialJSON(`{"flag":null,`);
    expect(r?.value).toEqual({ flag: null });
  });

  it("escape sequences inside strings parse correctly", () => {
    const r = parsePartialJSON(`{"label":"Hello \\"world\\""`);
    expect(r?.value).toEqual({ label: 'Hello "world"' });
  });
});
