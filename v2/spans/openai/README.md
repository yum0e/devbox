# OpenAI Span

Grant: authenticated ChatGPT/Codex `GET` and `POST` requests below
`https://chatgpt.com/backend-api/codex`.

Deny: the bearer token, account ID, CA private key, `api.openai.com`, arbitrary
URLs, raw-token operations, and authentication refresh from the Island.

The provider reads the host-managed Codex subscription login lazily and runs a
digest-pinned, non-root Iron helper. The client receives only a public CA and
creates child-scoped fake authentication for `openai run -- COMMAND`. The Tact
wrapper uses this client automatically.
