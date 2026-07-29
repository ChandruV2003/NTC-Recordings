# NTC Recordings

Public recording request form and internal approval panel for NTC Newark recordings.

This service indexes available message and worship recordings, accepts listener requests, creates private share links, and sends approval emails through the configured recordings email account.

## Recorder Review Ownership

Recorder Review is the human review and correction surface for shared recording
decisions. `NTC-Agent` owns the semantic decision contract and returns a
versioned classification, confidence, evidence, traits, speaker/title
suggestions, and similar completed reviews. Recorder ingest and publishing
pipelines own acquisition, normalization, silence analysis, splitting, file
movement, and recorder cleanup.

This service stores the Agent evidence beside each review row, presents it in
plain language, and records confirmed corrections. Upstream labels such as
`message_candidate` and `testimony_candidate` are hints only; they do not decide
the destination folder. Ambiguous, combined, or policy-disallowed recordings
remain in review instead of being filed automatically.

Missing and outdated analysis is selected from the complete review queue by a
single background worker. Opening Recorder Review, changing tabs, sorting, or
changing the row limit never starts analysis for the visible rows.

Completed Recorder Review actions store their source and completion time without
assigning rows to individual reviewers. Failed file moves remain unresolved and
do not overwrite the last successful review state.

Regular Sunday and Wednesday message coverage is checked internally from the
finalized library. Confirmed Convention and No Service exceptions remain in the
database, but coverage is not a separate admin workflow.

## Runtime

- Panel port: `1977` in-container, usually published as `7777`
- Entry point: `ntc_recordings_panel:app`
- Runtime database and indexes live under `data/` and are not committed
- Environment variables use the `NTC_RECORDINGS_*` and `NTC_NEXTCLOUD_*` prefixes
- `NTC_RECORDINGS_SERVICE_LEDGER_START_DATE` sets the first date considered by
  the internal regular-service coverage check.

## Local Validation

```bash
python3 -m py_compile ntc_recordings_app.py ntc_recordings_panel.py
python3 -m pytest test_ntc_recordings_panel.py
```
