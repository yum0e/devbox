# GitHub Span

Grant: token-authenticated HTTPS requests to `api.github.com` and
`uploads.github.com` from ordinary GitHub clients such as stock `gh`.

Deny: the OAuth token, CA private key, arbitrary destinations, Git HTTPS
credentials, and raw-token operations. Repository transport is intentionally
SSH and composes with the `ssh-agent` Span.

The GitHub World reads devc2's host-managed `gh` login lazily and runs a
digest-pinned, non-root Iron helper. It returns only a bounded manifest containing
fake credentials, public CA material, and the two permitted routes. The generic
HTTP Link attaches that manifest for the lifetime of the granted Island.

There is no GitHub-specific Link or wrapper in the Island.
