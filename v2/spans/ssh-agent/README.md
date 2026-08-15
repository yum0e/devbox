# SSH-agent Span

Grant: identity discovery and arbitrary signature requests for exactly one
host-selected SSH public key.

Deny: every other key and SSH-agent operation, including add/remove/lock and
session-bind extensions. The raw host agent socket and all private key material
remain on the host.

The SSH World itself speaks the standard SSH-agent protocol. The existing generic
opaque-stream Link projects it as `/run/devc2/spans/ssh-agent.sock`, and the
Island points `SSH_AUTH_SOCK` there directly. No command, SSH-specific client,
second agent proxy, or adapter runs inside the Island.
