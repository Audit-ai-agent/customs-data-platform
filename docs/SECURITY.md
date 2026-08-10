# Security and local data handling

- Never commit `.env`, `_local`, `_artifacts`, token files, API keys, DSNs,
  downloaded model files, raw official responses, or UAT output.
- Profile initializers create random local database passwords and one Data ↔
  Agent bearer token under `_local/profiles/<profile>/`; commands report only
  a classification when that input is missing or malformed.
- The Data stores and Evidence server bind to literal loopback addresses.
  Agent consumes only the authenticated V3 HTTP boundary and never opens a
  Data store directly.
- `functional-uat-law-history` and the retained `functional-uat-3law` accept
  a personal `LAW_API_OC` only in their own ignored local profile config. It
  is neither committed nor logged, copied from another profile, nor read from
  a clone checkout.
- Suspected credential exposure requires the owner to revoke and replace the
  value. Do not copy the value into source control, logs, pull requests, or
  issues.
