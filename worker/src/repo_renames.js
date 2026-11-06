// Old name to current repo. Laptops keep reporting a stale remote until someone updates it, and a
// deleted worktree only has its old folder name. Add an entry whenever an org repo is renamed.
export const REPO_RENAMES = {
  "your-org/example-app-legacy": "your-org/example-app", // example: replace with your own renames
};

export const currentRepo = (repo) => REPO_RENAMES[repo] ?? repo;
