# OpenAI Span

Grant: authenticated ChatGPT/Codex `GET` and `POST` requests below
`https://chatgpt.com/backend-api/codex`.

Deny: the bearer token, account ID, CA private key, `api.openai.com`, arbitrary
URLs, raw-token operations, and authentication refresh from the Island.

The OpenAI World reads the host-managed Codex subscription login lazily and runs
a digest-pinned, non-root Iron helper. It exports a bounded scoped-exec manifest:
public CA, fake child authentication, exact `chatgpt.com:443` route, and scoped
environment. devc2 resolves that World to its generic scoped-exec Link; OpenAI
supplies no Island client code. The Tact wrapper invokes the projected Link as
`openai run -- COMMAND`.

The generic Link runs exact argv inside the Island with inherited stdio, forwards
signals, streams declared CONNECT traffic to the World, preserves ordinary child
HTTPS routing, and removes all temporary state. The World independently enforces
the exact host, methods, Codex path prefix, and required placeholder headers.

Subscription renewal is deliberately host-only. The World rejects a managed
access token expiring within one hour; refresh tokens never cross the Link.
