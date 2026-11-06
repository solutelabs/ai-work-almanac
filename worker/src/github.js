const API = "https://api.github.com";

export class EnrollError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/** Runs every task even if one fails, then throws the first failure so it still shows in the Worker's logs. */
export async function settleAll(tasks) {
  const results = await Promise.allSettled(tasks);
  const failed = results.filter((r) => r.status === "rejected");
  for (const r of failed) console.error(r.reason);
  if (failed.length) throw failed[0].reason;
}

/** Asks GitHub Actions in `repo` (owner/name) to run a workflow file (the git upload by default). Throwing makes the failure show in the Worker's logs. */
export async function startGitUpload(token, repo, fetcher = fetch, workflow = "nightly.yml") {
  if (!token) throw new Error(`GIT_UPLOAD_TOKEN isn't set, so ${workflow} wasn't started`);
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo ?? "")) throw new Error(`UPLOAD_REPO must be owner/name, so ${workflow} wasn't started`);
  const response = await fetcher(`${API}/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, accept: "application/vnd.github+json", "user-agent": "pulse-tracker" },
    body: JSON.stringify({ ref: "main" }),
  });
  if (!response.ok) throw new Error(`GitHub refused to start ${workflow}: ${response.status}`);
}

/** Uses a developer's GitHub sign-in to prove who they are and that they belong to an allowed org. */
export async function githubIdentity(token, orgs, fetcher = fetch) {
  if (typeof token !== "string" || !token || token.length > 512) throw new EnrollError("invalid GitHub token", 400);
  const get = (path) => fetcher(API + path, {
    headers: { authorization: `Bearer ${token}`, accept: "application/vnd.github+json", "user-agent": "pulse-tracker" },
  });

  const userResponse = await get("/user");
  if (userResponse.status === 401) throw new EnrollError("GitHub rejected this sign-in. Run the install again.", 401);
  if (!userResponse.ok) throw new EnrollError("GitHub is unavailable. Try again shortly.", 502);
  const user = await userResponse.json();

  let member = false;
  for (const org of orgs) {
    const response = await get(`/user/memberships/orgs/${encodeURIComponent(org)}`);
    if (response.status >= 500) throw new EnrollError("GitHub is unavailable. Try again shortly.", 502);
    if (response.ok && (await response.json()).state === "active") member = true;
  }
  if (!member) throw new EnrollError(`${user.login} isn't an active member of ${orgs.join(", ")} on GitHub.`, 403);

  const emailResponse = await get("/user/emails");
  if (!emailResponse.ok) throw new EnrollError("Could not read your verified GitHub emails. Run the install again.", 502);
  const verified = (await emailResponse.json()).filter((e) => e.verified);
  const primary = verified.find((e) => e.primary) ?? verified[0];
  if (!primary) throw new EnrollError("Your GitHub account has no verified email.", 403);

  const login = user.login.toLowerCase();
  const emails = verified.map((e) => e.email.toLowerCase());
  return {
    login,
    name: user.name?.trim() || user.login,
    email: primary.email.toLowerCase(),
    emails,
    identityKeys: [login, ...emails],
  };
}
