# Tennessee -> Texas adjustment register

Vendored 2026-08-20 from Ty's upstream (Knox/Blount County TN operation). This file
tracks every market-specific item so the manual pass is recorded, not remembered.
Baseline before the pull: tag `pre-upstream-vendor-2026-08-20`.

## A. Must adjust BEFORE first use in our market

### vendor-directory-builder (new skill — method is market-neutral, examples are not)
`SKILL.md` has ZERO TN references. Only the worked example needs swapping:

| File | Lines | What | TX replacement |
|---|---|---|---|
| `skills/vendor-directory-builder/assets/example_config.json` | 4, 9, 41, 48-49, 54-55, 65 (17 refs) | Knox+Blount worked example: market name, counties, FB group, utility districts | Travis/Bell/Williamson config; source the community from a Central TX investor FB group; gatekeeper layer = Austin Energy / Austin Water / Oncor / MUDs + Travis-Bell-Wilco permit offices |
| `skills/vendor-directory-builder/evals/evals.json` | 13-14, 19-20 | Eval fixtures name Knox market | Re-point at a TX market (or leave — evals only run upstream) |
| `skills/vendor-directory-builder/assets/config_schema.md` | 2 refs | Doc examples | Cosmetic; fix when touched |
| `skills/contractor-call-sheet/` | 2 refs (SKILL.md + build_call_sheet.py) | Example strings | Cosmetic; fix when touched |

Mirror any edits into `~/.claude/skills/{vendor-directory-builder,contractor-call-sheet}/`
(installed copies) — or edit installed first and copy back.

### fly.toml / fly.ftm.toml (inert until a `fly deploy`, but wrong-market as shipped)
| Line | Shipped | For us |
|---|---|---|
| `fly.toml:19` | `SMS_AGENT_LOCALITY = "Blount County"` | Our county per deployment |
| `fly.ftm.toml:15` | `BUSINESS_TIMEZONE = 'America/New_York'` | `America/Chicago` |
| `fly.ftm.toml:14` | `APIFY_PROXY_SESSION = 'tnpn_fly'` | Our own session name |
| both | `primary_region = 'iad'` + app names `siftstack`/`siftstack-ftm` | Region fine; app names collide with TY'S LIVE APPS — must rename before any deploy |

### .github/workflows.upstream-parked/ (deliberately NOT in workflows/)
`sms-agent-heartbeat.yml` polls **Ty's** production app (`siftstack.fly.dev`) every
5 minutes and posts to a Slack webhook. Never enable unedited. See that dir's README.

## B. Pre-existing TN content in already-installed skills — unchanged by this pull
Recorded for completeness; do NOT "fix" as part of the vendoring (several of these
skills carry local TX patches; edits go through the normal skill-update flow):

| Skill | TN refs | Notes |
|---|---|---|
| buyer-prospector | 741 | Knox worked examples throughout |
| sift-market-research | 80 | (our installed copy already diverges from upstream) |
| first-market-county-data | 77 | |
| rehab-estimator | 39 | The LOCKED Knox 37914 Home Depot master list — src side already relocked to Austin 78704 (commit 7ffb84a); the skill's bundled list still Knox |
| deep-prospecting-v5 | 23 | |
| real-estate-comping | 9 | |
| comp-package | 8 | |

## C. Vendored verbatim, TN-flavored, intentionally untouched
- `archive/ty_operator_scripts/` — 52 Knox one-offs, parked out of src/ (see its README)
- `dist/*.skill` ZIPs — upstream-built artifacts (deep-prospecting-v5.skill still
  contains the retired tracerfy reference internally; source tree does not)
- `docs/contractor-research-workflow.md` — Ty's SOP with the Knox+Blount worked example;
  useful as the method doc regardless of market
