# Span gateway

The gateway is the Box-side adapter for explicitly granted Spans. It exposes
opaque World streams as Unix sockets and routes declared HTTPS authorities
through an HTTP CONNECT proxy. It never receives capability credentials or
inspects tunneled TLS payloads; it knows Span names, public routes, ephemeral
relay ports, and per-launch mTLS transport material.
