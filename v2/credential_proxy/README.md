# Span bridge

This image contains only the unprivileged, protocol-agnostic bridge that projects
explicitly granted host Span streams as Unix sockets inside an Island. It never
receives capability credentials or World configuration; it knows only Span
names, ephemeral loopback relay ports, and per-launch mTLS transport material.
