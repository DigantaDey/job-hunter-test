# Security policy

The security model, threat considerations and accepted trade-offs are documented in
[`docs/SECURITY.md`](docs/SECURITY.md). Deployment hardening steps are in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Reporting a vulnerability

Please report suspected vulnerabilities privately — use GitHub's **Report a vulnerability**
(Security → Advisories) on this repository, or email the maintainer address listed on the
repository profile. Do **not** open a public issue for anything exploitable.

Include: affected version/commit, reproduction steps, impact assessment, and any suggested fix.
We aim to acknowledge within 3 business days and to ship a fix or mitigation plan within 30 days
for confirmed high-severity issues.

## Supported versions

| Version | Supported |
|---|---|
| 2.0.x | ✅ security fixes |
| < 2.0 | ❌ (upgrade — the 1.x line ships with default secrets and no authentication) |

## Operator responsibilities

* Set strong `SECRET_KEY`/`ENCRYPTION_KEY` values and store them in a secret manager.
* Terminate TLS in front of the app and restrict `/api/metrics` with `METRICS_TOKEN`.
* Keep the container image and pinned dependencies updated (Dependabot PRs are enabled).
* Take and restore-test backups (`backend/scripts/backup.py`).
* Only enable real email sending and browser automation after your own risk review — both are off
  by default and are audited when changed.
