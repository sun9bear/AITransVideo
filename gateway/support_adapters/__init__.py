"""External handoff providers.

Each module in this package implements the
``support_handoff.HandoffAdapter`` protocol. P1 ships with:

- ``email`` — the default; logs structured ticket emails (real SMTP
  wiring deferred — operator runs on logs initially).
- ``chatwoot`` — stub. Plan §9.2 says we deploy Chatwoot only after the
  email channel reveals real ticket volume.
- ``wechat_kf`` — stub. Plan §9.4 splits into a P4 link entrypoint and
  a P5 API integration. Neither is wired up here.
"""
