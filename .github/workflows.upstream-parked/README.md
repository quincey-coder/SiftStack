# Parked upstream workflows (NOT active)

GitHub only runs workflows from `.github/workflows/`; this directory is inert on
purpose. Vendored 2026-08-20 from upstream/main:

- `sms-agent-heartbeat.yml` — cron every 5 min, curls TY'S production Fly app
  (https://siftstack.fly.dev/alive) and posts to a Slack webhook. NEVER enable
  as-is: it would poll someone else's box from our repo. Repoint the URL +
  webhook to our own deployment first.
- `skills.yml` / `docs.yml` — build the skill ZIPs and publish docs/GitHub Pages
  on push. Enable only after deciding we want CI builds + a Pages site on this
  fork.

To activate one: fix its targets, then `git mv` it into `.github/workflows/`.
