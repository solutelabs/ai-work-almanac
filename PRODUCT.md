# Product

## Platform

web

## Users

Leadership and technical project managers. The dashboard is limited to a Google sign-in allow list, and it shows named data about each person.

## Product Purpose

Shows how people in a team work with AI tools across projects, and what that work produces. The first thing it must answer is how people work with AI: who uses which tools, how much, and what comes out.

## Operating Context

Data comes from an hourly collector on each laptop (Claude Code and Codex usage, commits, prompts and harness setup) and from GitHub (commits and pull requests). Some developers run autonomous agents under their own account, so an account's output isn't one person's effort. Sign-in is Google, through Cloudflare Access.

## Capabilities and Constraints

- A single self-contained HTML page served by a Cloudflare Worker. Its content security policy blocks external scripts, styles and fonts.
- Filters by period, tool, repository and developer.
- Not everyone has installed the collector: no data is not the same as no AI use, and must read differently.
- Codex logs have no line counts.
- The install notice says what is collected, and new versions wait for a re-install if it changes. Prompts, responses, commands and file paths are collected. File contents and command output aren't. Only the people the notice names can read prompt text. Repo names are collected, including personal and client repos.

## Brand Commitments

Must not feel like a generic SaaS analytics template.

## Evidence on Hand

Live data in the `tracker` schema. No logo or brand assets are in this repo.

## Product Principles

- Understanding how work happens comes before measuring individuals.
- Show only what points to a decision or a question worth asking.
- Say what a number can't tell you, where it could mislead.
