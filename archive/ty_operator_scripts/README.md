# Ty's one-off operator scripts (vendored 2026-08-20)

52 single-purpose scripts from upstream/main `src/`, vendored verbatim during the
2026-08-20 upstream pull. Nearly all are Knox/Blount County TN one-offs (named
records, single-run skip traces, probes). They are parked here — NOT in `src/` —
so they can't be mistaken for live pipeline modules or imported by `main.py`.

Market-neutral pieces worth mining before writing something new:
`heir_signing_workflow.py`, `sift_upload_wizard.py`, `enformion_heir.py`,
`deep_prospect_pdf.py`, `scrapfly_browser.py`.

Anything promoted back into `src/` must be TX-adapted first — see
docs/TN_TO_TX_ADJUSTMENTS.md.
