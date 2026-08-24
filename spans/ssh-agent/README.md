# SSH-agent Span

Grant: identity discovery and arbitrary signature requests for exactly one
host-selected SSH public key.

Deny: every other key and SSH-agent operation, including add/remove/lock and
session-bind extensions. The raw host agent socket and all private key material
remain on the host.

The SSH World itself speaks the standard SSH-agent protocol. The existing generic
opaque-stream Link projects it as `/run/devc2/spans/ssh-agent.sock`, and the
Island points `SSH_AUTH_SOCK` there directly. No command, SSH-specific protocol
client, or second agent proxy runs inside the Island. Entrypoint creates one
five-line `ssh-keygen` launcher for Git and Jujutsu because agent runtimes may
remove `SSH_AUTH_SOCK` from child commands; it only restores this fixed projected
socket and executes the system binary.

Only the inherited Link listener is validated during startup. The selected
public key is loaded on the first request and then fixed for that World process;
host-agent availability is evaluated on every request. Missing configuration or
a temporarily unavailable identity returns a normal SSH-agent failure but does
not kill the World, so a later request can recover without restarting the
Island.
