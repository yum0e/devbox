# Credential boundary

GitHub OAuth and ChatGPT credentials are substituted by the pinned iron-proxy
service from private, read-only files created by the trusted launcher. The devbox
contains only placeholder credentials and the ephemeral CA certificate.

`ssh_agent_proxy.py` is the separate filtered SSH-agent sidecar. The raw Docker
Desktop/1Password agent socket is mounted only into this network-disabled service.
It advertises one configured public key and forwards only identity queries and
signing requests for that exact key. The private key never leaves 1Password.

These controls prevent direct credential extraction. They do not restrict use:
while running, untrusted code can exercise the full GitHub OAuth authority and can
request arbitrary signatures from the selected SSH key.
