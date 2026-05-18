"""Pan backup subsystem (admin-only feature, plan 2026-05-13 / impl 2026-05-14).

Public modules:
- token_crypto: Fernet symmetric encrypt/decrypt for OAuth tokens
- (additional modules added by later tasks: baidu_pan_client, manifest,
  status_mutator, archive_scanner, orphan_cleanup, stale_reaper, auth)
"""
