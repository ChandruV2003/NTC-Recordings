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

Completed Recorder Review actions also store the reviewer, source, and
completion time. Failed file moves remain unresolved and do not overwrite the
last successful review provenance.

## Service Coverage

The admin Service Coverage screen derives expected regular services from Sunday
and Wednesday dates and derives completion from finalized message recordings in
the library. The database stores only explicit exceptions such as Convention or
No Service, including who confirmed the exception and when.

## Runtime

- Panel port: `1977` in-container, usually published as `7777`
- Entry point: `ntc_recordings_panel:app`
- Runtime database and indexes live under `data/` and are not committed
- Environment variables use the `NTC_RECORDINGS_*` and `NTC_NEXTCLOUD_*` prefixes
- `NTC_RECORDINGS_DEFAULT_REVIEWER_NAME` supplies the login form's default
  reviewer identity.
- `NTC_RECORDINGS_SERVICE_LEDGER_START_DATE` sets the first date considered by
  Service Coverage.

## Local Validation

```bash
python3 -m py_compile ntc_recordings_app.py ntc_recordings_panel.py
python3 -m pytest test_ntc_recordings_panel.py
```
