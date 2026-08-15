# Span bridge

This image contains only the unprivileged, protocol-agnostic bridge that projects
explicitly granted host Span streams as Unix sockets inside an Island. It never
receives capability credentials or provider configuration; it knows only Span
names, ephemeral loopback relay ports, and per-launch mTLS transport material.

`ssh_agent_proxy.py` is a nonfunctional marker retained for one release so the
previous updater can validate and import this asset tree. No runtime references it.
