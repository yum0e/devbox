# OpenAI Span

Grant: authenticated ChatGPT/Codex `GET` and `POST` requests below
`https://chatgpt.com/backend-api/codex`.

Deny: the bearer token, account ID, CA private key, `api.openai.com`, arbitrary
URLs, raw-token operations, and authentication refresh from the Island.

The OpenAI World reads the host-managed Codex subscription login lazily and runs
a digest-pinned, non-root Iron helper. It exports a bounded attachment manifest:
public CA, fake authentication, exact `chatgpt.com:443` route, and environment.
devc2 resolves that World to its generic HTTP Link; OpenAI supplies no Island
client code. Stock Tact and Pi receive only the same fake bearer used by the
proxy, never the host token.

The generic Link streams declared CONNECT traffic to the World, preserves
ordinary HTTPS routing, and removes all attachment state with the Island. The World independently enforces
the exact host, methods, Codex path prefix, and required placeholder headers.

Subscription renewal is deliberately host-only. The World rejects a managed
access token expiring within one hour; refresh tokens never cross the Link.
