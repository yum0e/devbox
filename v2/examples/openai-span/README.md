# OpenAI Span

This experimental Span lets an Island run Tact with host-held ChatGPT/Codex
credentials. The real bearer token and account ID never enter the Island.

```text
                         HOST

  managed auth                 OpenAI Span provider
  ┌──────────────┐             ┌────────────────────────┐
  │ real bearer  ├────────────►│ pinned Iron proxy      │────► chatgpt.com
  │ account ID   │             │ exact host/path/method │
  └──────────────┘             └───────────▲────────────┘
                                          │ opaque CONNECT stream
  ═══════════════════════ ISLAND BOUNDARY ╪════════════════════════
                                          │
                              ┌───────────┴────────────┐
                              │ `openai run -- tact …` │
                              │ fake auth · public CA  │
                              │ loopback proxy         │
                              └────────────────────────┘

  grant: authenticated ChatGPT/Codex requests
  deny:  bearer token · account ID · CA key · arbitrary destination
```

The client environment and loopback adapter are child-scoped. They fetch only
the provider's public CA, create fake auth, run one command, and remove that
temporary state when the command exits. Like every Span grant, the authority is
Island-wide: any Island process may invoke the client, and the lazily started
host helper lives until the Island exits. The provider accepts only `CONNECT
chatgpt.com:443`; its pinned Iron helper injects credentials only for `GET` and `POST` requests to
`/backend-api/codex` and descendants when both exact placeholders are present.

## Install and run

First configure the existing host login with `devc2 auth`. Then, on the host:

```sh
./v2/examples/openai-span/install.sh
devc2 run . --span openai
```

Inside the Island:

```sh
openai run -- tact run 'Reply with exactly: OPENAI SPAN OK'
```

Spans compose without knowing about one another. With the Herdr Span granted as
well, a host-visible agent pane is just:

```sh
herdr spawn reviewer -- openai run -- tact run 'Review the current diff.'
```

This example intentionally has no raw-token operation, arbitrary URL, API-key
support, refresh protocol, or `api.openai.com` authority.

The current v2 Island still starts its built-in credential proxy. This additive
example validates the replacement boundary first; removing the built-in
OpenAI-specific path comes only after the Span succeeds end to end.
