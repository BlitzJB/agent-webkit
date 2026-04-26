import { expect, test } from "@playwright/test";

test("send a message and receive an assistant reply", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /chat demo/i })).toBeVisible();

  const input = page.getByPlaceholder("Type a message…");
  await input.fill("hi");
  await page.getByRole("button", { name: "Send" }).click();

  // The fake fixture (plain_qa.jsonl) replies with "hello world".
  await expect(page.getByText("hello world")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("you:")).toBeVisible();
});
