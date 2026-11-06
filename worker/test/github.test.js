import assert from "node:assert/strict";
import test from "node:test";
import { currentRepo } from "../src/repo_renames.js";
import { githubIdentity, settleAll, startGitUpload } from "../src/github.js";

const reply = (body, status = 200) => new Response(JSON.stringify(body), { status });

function github(routes) {
  return async (url, options) => {
    assert.equal(options.headers.authorization, "Bearer gho_x");
    const answer = routes[new URL(url).pathname];
    return answer ? answer() : reply({}, 404);
  };
}

const member = {
  "/user": () => reply({ login: "Asha-SL", name: "Asha Rao" }),
  "/user/memberships/orgs/your-org": () => reply({ state: "active" }),
  "/user/emails": () => reply([
    { email: "personal@gmail.com", verified: true, primary: false },
    { email: "Asha@Example.com", verified: true, primary: true },
    { email: "someone-else@example.com", verified: false, primary: false },
  ]),
};

test("starts the upload workflow on main, and fails loudly otherwise", async () => {
  let call;
  await startGitUpload("tok", async (url, options) => { call = { url, ...options }; return new Response(null, { status: 204 }); });
  assert.match(call.url, /\/repos\/your-org\/ai-work-almanac\/actions\/workflows\/nightly\.yml\/dispatches$/);
  assert.equal(call.method, "POST");
  assert.equal(call.headers.authorization, "Bearer tok");
  assert.deepEqual(JSON.parse(call.body), { ref: "main" });
  await assert.rejects(startGitUpload("tok", async () => new Response(null, { status: 403 })), /403/);
  await assert.rejects(startGitUpload(undefined, async () => assert.fail("must not call GitHub")), /GIT_UPLOAD_TOKEN/);
});

test("can start the status workflow instead", async () => {
  let url;
  await startGitUpload("tok", async (u) => { url = u; return new Response(null, { status: 204 }); }, "status.yml");
  assert.match(url, /\/actions\/workflows\/status\.yml\/dispatches$/);
});

test("settleAll runs every task even when one fails, then throws the failure", async () => {
  const ran = [];
  await assert.rejects(settleAll([Promise.reject(new Error("status failed")), Promise.resolve().then(() => ran.push("digest"))]), /status failed/);
  assert.deepEqual(ran, ["digest"]);
  await assert.rejects(settleAll([Promise.resolve(), Promise.reject(new Error("digest failed"))]), /digest failed/);
  await settleAll([Promise.resolve()]);
  const logged = [];
  const original = console.error;
  console.error = (e) => logged.push(e.message);
  try {
    await assert.rejects(settleAll([Promise.reject(new Error("a")), Promise.reject(new Error("b"))]), /a/);
  } finally {
    console.error = original;
  }
  assert.deepEqual(logged, ["a", "b"]);
});

test("an active org member gets an identity built only from verified emails", async () => {
  const id = await githubIdentity("gho_x", ["your-org"], github(member));
  assert.equal(id.email, "asha@example.com");
  assert.deepEqual(id.identityKeys, ["asha-sl", "personal@gmail.com", "asha@example.com"]);
  assert.equal(id.name, "Asha Rao");
});

test("falls back to the login when the profile has no name", async () => {
  const id = await githubIdentity("gho_x", ["your-org"], github({ ...member, "/user": () => reply({ login: "asha", name: null }) }));
  assert.equal(id.name, "asha");
});

test("refuses people outside the org, and invited members who haven't accepted", async () => {
  await assert.rejects(githubIdentity("gho_x", ["your-org"], github({ ...member, "/user/memberships/orgs/your-org": () => reply({}, 404) })), { status: 403 });
  await assert.rejects(githubIdentity("gho_x", ["your-org"], github({ ...member, "/user/memberships/orgs/your-org": () => reply({ state: "pending" }) })), { status: 403 });
});

test("refuses bad tokens and accounts with no verified email", async () => {
  await assert.rejects(githubIdentity("gho_x", ["your-org"], github({ "/user": () => reply({}, 401) })), { status: 401 });
  await assert.rejects(githubIdentity("", ["your-org"], github(member)), { status: 400 });
  await assert.rejects(githubIdentity(42, ["your-org"], github(member)), { status: 400 });
  await assert.rejects(githubIdentity("gho_x", ["your-org"], github({ ...member, "/user/emails": () => reply([{ email: "a@b.co", verified: false }]) })), { status: 403 });
});

test("reports a GitHub outage as temporary instead of denying membership", async () => {
  await assert.rejects(githubIdentity("gho_x", ["your-org"], github({ ...member, "/user/memberships/orgs/your-org": () => reply({}, 503) })), { status: 502 });
});

test("a github SSH alias in an older laptop's repo name counts as our org's repo", () => {
  assert.equal(currentRepo("github.com-work-your-org/example-app"), "your-org/example-app");
  assert.equal(currentRepo("github-work-your-org/example-app"), "your-org/example-app");
  assert.equal(currentRepo("your-org/example-app"), "your-org/example-app");
  assert.equal(currentRepo("your-org/example-app-legacy"), "your-org/example-app");
  assert.equal(currentRepo("gitlab.com-your-org/example-app"), "gitlab.com-your-org/example-app");
  assert.equal(currentRepo("github.com-work-acme/app"), "github.com-work-acme/app");
});
