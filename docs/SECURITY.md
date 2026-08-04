# Security and data handling

- Never commit `.env`, token files, API keys, DSNs, downloaded raw source data,
  or UAT results. Keep them in the local workspace boundary.
- `.env.example` contains only local placeholders. External API credentials are
  injected by the operator process and are never printed by preflight checks.
- `law_repository/docker-compose.yml` publishes database ports only on
  `127.0.0.1`. Do not change that to a public binding for a demo.
- Agent code consumes the typed Evidence HTTP API only; it never connects to
  PostgreSQL, Qdrant, or Neo4j directly.
- `src.webapp` is an unauthenticated local demo. The authenticated runtime
  factory requires an explicit bearer credential and transport mode.
- Provider build headers and context digests establish byte compatibility; they
  are not credentials.
- A suspected credential exposure requires revocation and replacement by the
  credential owner. Do not paste the value into commits, logs, pull requests,
  or issues while investigating.

This repository does not contain cloud deployment recipes, container images,
release bundles, or GitHub Actions secrets. Authorized local UAT receives its
operator inputs separately from Git.
