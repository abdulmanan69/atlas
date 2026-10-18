# ADR 0002 — Token strategy and tenant resolution

**Status:** accepted · 2026-10-18

## Decision
* **Access token**: HS256 JWT, 15-minute TTL, kept in memory by the SPA, sent as
  `Authorization: Bearer`. Claims carry `sub` (user) and `sid` (session). Every request
  re-checks the session row, so revocation is immediate rather than waiting for expiry.
* **Refresh token**: 48-byte opaque random value in an `HttpOnly; SameSite=Strict` cookie
  scoped to `/api/v1/auth`. Only its SHA-256 is stored. Each use rotates it; presenting an
  already-rotated token revokes the entire token family (theft detection).
* **CSRF**: cookie-authenticated endpoints (`/auth/refresh`, `/auth/logout`) require a
  custom `X-Requested-With` header, which cross-site forms cannot send and CORS forbids.
  All other endpoints use bearer tokens, which browsers never attach automatically.
* **Passwords**: Argon2id (argon2-cffi defaults), minimum 12 characters, timing-equalized
  verification for unknown users, lockout for 15 minutes after 5 failures, Redis rate limits
  per IP and per email (fail open if Redis is down; lockout still applies).
* **Tenant**: chosen per request via `X-Organization-Id`, verified against membership.
  The organization id is never read from request bodies.

## Deferred
* SSO (OIDC/SAML) for enterprise customers — planned behind the same session model.
* PostgreSQL row-level security as a second line of defence. It needs a per-transaction
  `SET LOCAL app.org_id`, which fits our request-scoped sessions; to be added with tests
  once the schema stabilizes.
* Asymmetric (RS256/EdDSA) signing with key rotation when tokens must be verified by
  services other than the API.
