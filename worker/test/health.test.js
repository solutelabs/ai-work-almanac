import assert from "node:assert/strict";
import { timingSafeEqual } from "node:crypto";
import test from "node:test";
import { adminAuthorized } from "../src/auth.js";
import { buildHealth, handleHealth } from "../src/health.js";

// Workers has timingSafeEqual on crypto.subtle; Node keeps it in node:crypto.
crypto.subtle.timingSafeEqual ??= (a, b) => timingSafeEqual(a, b);

const asking = (authorization) => new Request("https://x/api/admin/health", { headers: authorization ? { authorization } : {} });

test("the admin token opens the route, and nothing else does", async () => {
  const env = { ADMIN_TOKEN: "secret" };
  assert.equal(await adminAuthorized(asking("Bearer secret"), env), true);
  assert.equal(await adminAuthorized(asking("Bearer wrong"), env), false);
  assert.equal(await adminAuthorized(asking(), env), false);
  assert.equal(await adminAuthorized(asking("Basic c2VjcmV0"), env), false);
});

test("no configured token means no access", async () => {
  assert.equal(await adminAuthorized(asking("Bearer x"), {}), false);
  assert.equal(await adminAuthorized(asking(), {}), false);
});

test("bad or missing values come back as null, not as a time or zero", () => {
  const h = buildHealth({ gitSyncedAt: "nonsense", laptopSeen: null, laptopsLastDay: -1 }, 0);
  assert.deepEqual(h, { now: "1970-01-01T00:00:00.000Z", git_synced_at: null, last_laptop_seen: null, laptops_seen_24h: null });
  assert.equal(buildHealth({ gitSyncedAt: undefined, laptopSeen: 5, laptopsLastDay: "3" }).last_laptop_seen, null);
});

test("good values pass through", () => {
  const h = buildHealth({ gitSyncedAt: "2026-11-05T10:00:00Z", laptopSeen: "2026-11-05T09:00:00.000Z", laptopsLastDay: 0 });
  assert.equal(h.git_synced_at, "2026-11-05T10:00:00.000Z");
  assert.equal(h.laptops_seen_24h, 0);
});

test("the answer holds only timestamps and counts, even if rows carry names", async () => {
  const sql = async (strings) => (strings.join("").includes("git_activity")
    ? [{ at: "2026-11-05T10:00:00Z", person: "Asha" }]
    : [{ seen: "2026-11-05T09:00:00Z", recent: 2, developer: "Asha", email: "a@x.com" }]);
  const response = await handleHealth(sql);
  const body = await response.json();
  assert.deepEqual(Object.keys(body).sort(), ["git_synced_at", "laptops_seen_24h", "last_laptop_seen", "now"]);
  assert.doesNotMatch(JSON.stringify(body), /Asha|@/);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("an empty database reports no data", async () => {
  const body = await (await handleHealth(async () => [{}])).json();
  assert.equal(body.git_synced_at, null);
  assert.equal(body.last_laptop_seen, null);
});
