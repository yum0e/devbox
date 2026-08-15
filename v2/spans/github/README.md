# GitHub Span

Grant: token-authenticated HTTPS requests to `api.github.com` and
`uploads.github.com` through `github run -- COMMAND`.

Deny: the OAuth token, CA private key, arbitrary destinations, Git HTTPS
credentials, and raw-token operations. Repository transport is intentionally
SSH and composes with the `ssh-agent` Span.

The provider reads devc2's host-managed `gh` login lazily and runs a
digest-pinned, non-root Iron helper. The client creates a child-scoped fake
`GH_TOKEN`, public CA bundle, proxy, and `gh` configuration, then removes them.
