import { expect, test } from "@playwright/test";

/**
 * Full-stack E2E for generative UI:
 *
 *  Python server (fake SDK + GenUIRegistry)
 *    ↓ HTTP+SSE
 *  React app (useAgentSession + useGenerativeUI)
 *    ↓ event tap
 *  GenUIStream → renderer registry → WeatherCard React component → DOM
 *
 * The fake fixture (`fixtures/genui_render.jsonl`) emits a tool_use for
 * `mcp__genui__render_weather_card` with `location: "Boston, MA"`. The
 * frontend's WeatherCard renderer puts that string in the DOM. We assert it.
 */
test("a tool_use event end-to-end mounts the corresponding React renderer", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /generative ui demo/i })).toBeVisible();

  // The renderer count comes from the schema fetch — proves /genui/schema worked.
  await expect(page.getByText(/2 renderers loaded/i)).toBeVisible({ timeout: 10_000 });

  const input = page.getByPlaceholder("Ask the agent to render something…");
  await input.fill("Render Boston weather");
  await page.getByRole("button", { name: "Send" }).click();

  // Asserting on the WeatherCard's specific DOM:
  // 1. The location string from the props arrives intact.
  await expect(page.getByText("Boston, MA")).toBeVisible({ timeout: 10_000 });
  // 2. The numeric temperature_f rounded into the rendered display.
  await expect(page.getByText("72°F")).toBeVisible();
  // 3. The optional condition field also flowed through.
  await expect(page.getByText("sunny")).toBeVisible();
});
