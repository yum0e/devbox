# Credential boundary

GitHub OAuth and ChatGPT credentials are substituted by the pinned iron-proxy
service from private, read-only files created by the trusted launcher. The devbox
contains only placeholder credentials and the ephemeral CA certificate.

`ssh_agent_proxy.py` runs selected-key filters on both sides of an ephemeral mTLS
relay. The host filter is the only process that opens the ambient 1Password agent;
the raw socket never enters Docker. The container filter advertises one configured
public key and forwards only identity queries and signing requests for that exact
key. The relay CA and leaf certificates are generated per launch. Only the client
leaf material enters the SSH sidecar; neither it nor the private key is mounted in
the devbox. The selected SSH private key never leaves 1Password.

These controls prevent direct credential extraction. They do not restrict use:
while running, untrusted code can exercise the full GitHub OAuth authority and can
request arbitrary signatures from the selected SSH key.
