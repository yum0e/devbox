# SSH-agent Span

Grant: identity discovery and arbitrary signature requests for exactly one
host-selected SSH public key.

Deny: every other key and SSH-agent operation, including add/remove/lock and
session-bind extensions. The raw host agent socket and all private key material
remain on the host.

The Span endpoint itself speaks the standard SSH-agent protocol, so the Island
sets `SSH_AUTH_SOCK=/run/devc2/spans/ssh-agent.sock` directly. No second agent
proxy or Span-specific adapter runs inside the Island.
