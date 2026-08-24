# Span bridge

This image contains an unprivileged bridge that projects explicitly granted host
Span streams as Unix sockets, plus an HTTP CONNECT router for public per-World
routes. It never receives capability credentials or inspects tunneled TLS
payloads; it knows Span names, routes, ephemeral relay ports, and per-launch mTLS
transport material.
