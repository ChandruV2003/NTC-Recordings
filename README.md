# NTC Recordings

Public recording request form and internal approval panel for NTC Newark recordings.

This service indexes available message and worship recordings, accepts listener requests, creates private share links, and sends approval emails through the configured recordings email account.

## Runtime

- Panel port: `1977` in-container, usually published as `7777`
- Entry point: `ntc_recordings_panel:app`
- Runtime database and indexes live under `data/` and are not committed
- Environment variables use the `NTC_RECORDINGS_*` and `NTC_NEXTCLOUD_*` prefixes

## Local Validation

```bash
python3 -m py_compile ntc_recordings_app.py ntc_recordings_panel.py
python3 -m pytest test_ntc_recordings_panel.py
```
