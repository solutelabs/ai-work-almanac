// Old name to current repo. Laptops keep reporting a stale remote until someone updates it, and a
// deleted worktree only has its old folder name. Add an entry whenever an org repo is renamed.
export const REPO_RENAMES = {
  "your-org/example-app-legacy": "your-org/example-app", // example: replace with your own renames
};

// An SSH alias like github.com-work reaches your org on GitHub, but older collectors put the alias in the name.
const GITHUB_ALIAS = /^github(?:\.com)?-.*-(your-org\/.+)$/;
export const currentRepo = (repo) => {
  const name = repo.match(GITHUB_ALIAS)?.[1] ?? repo;
  return REPO_RENAMES[name] ?? name;
};
