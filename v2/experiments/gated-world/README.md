# Gated World experiment

This setup tests whether a former “provider” is better modeled as an ordinary
World. Its Worlds are ordinary processes inside the environment running the
experiment; none is intrinsically an agent, container, or VM.

```text
Caller World                 Slack Gate World                 Slack World

`slack ...` ── Unix socket ─▶ parse + attenuate ── private FD ─▶ broad API
      │                              │                             │
 generic shim                 trusted gate code             accepts admin.export
 no upstream FD               holds both Links              no public endpoint
```

An independent signer World exports a native socket affordance:

```text
Caller World ── /run-like Unix socket ──▶ generic projection ── private FD ──▶ Signer World
```

The orchestrator creates a private socket pair and passes one end to Slack and
the other to the Gate. The upstream Link therefore has no socket pathname. The
Caller inherits neither endpoint and receives only the Gate's projected command
socket. Possession of the descriptor is the authority.

The fake Slack World deliberately accepts `admin.export`. The Gate exports only:

```text
slack channels list
slack messages read engineering
slack messages send engineering TEXT --confirm
```

Unknown operations fail closed. The projected `slack` command is only a symlink
to one generic `shima-link` executable; caller-controlled arguments are framed
as data and are never interpreted by a shell.

After binding its incoming endpoint and receiving its outgoing Link, the Gate
applies Landlock and seccomp. All new filesystem access, TCP access, executable
launch, process creation/signalling, namespace operations, and new sockets are
denied. An allowed Slack operation deliberately attempts filesystem, TCP, exec,
and process escapes; the orchestrator verifies that none succeeds. This is a
lean Linux process-World backend, not a requirement that every World use Linux.

Run the complete experiment directly from the repository root:

```sh
./v2/experiments/gated-world/test.sh
```

The test proves that allowed calls reach Slack, denied calls do not, the Caller
receives no direct upstream Link or signing secret, and the scoped write requires
explicit confirmation. It then independently revokes and regrants the signer
socket projection, revokes the Slack command projection, and proves the other
affordance remains usable each time.

Landlock and seccomp materially attenuate the Gate but are not a VM boundary.
A container, VM, SSH host, or another process sandbox can materialize the same
graph with different containment strength. Containment is a World-backend
property, not a Link, affordance, projection, or Gate property.

This intentionally does not define a Shima configuration format: the topology
is concrete until another experiment reveals what should be shared.
