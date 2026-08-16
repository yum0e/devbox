# Herdr Span

This command-Link experiment lets one Island create host-visible Herdr panes without
giving the Island Herdr's host socket or Docker. Every spawned command still
runs inside the same Island.

```text
                            HOST

 Island ── projected `herdr` ──▶ Herdr World ── pane operations ──▶ Herdr
                                      │
                              private worker Link
                                      │
                                      ▼
                                 Docker Agent
                                      │ fixed backend policy
                                      ▼
                         docker exec ──▶ same Island

 Herdr World: pane ownership · names · read/send/wait · no Docker knowledge
 Docker Agent: exact Island selection · fixed user/cwd/TTY · no Herdr authority
 Island:       no Docker socket · no host Herdr socket · no worker Link
```

One deterministic executable bundle keeps installation and catalog lookup atomic,
but runs a composition root, Herdr World, and Docker Agent as separate processes
with allowlisted environments. The catalog and generic projected command Link
remain unchanged. Backend complexity stays behind the private worker Link.

## Install and run

Herdr must already be running on the host. From a host Herdr pane:

```sh
./v2/examples/herdr-span/install.sh
devc2 run . --span herdr
```

The World starts independently of Herdr's current health. Each projected command
checks the pane inherited from the host launch, reports a bounded error if
Herdr is unavailable, and retries that check on the next call. Calls are
rejected if `devc2` was not launched from a Herdr-managed pane.

Inside the Island:

```sh
herdr --help
herdr spawn proof -- sh -lc 'hostname; test ! -S /var/run/docker.sock; sleep 2'
herdr list
herdr read proof
herdr wait proof
```

For the real proof:

```sh
herdr spawn reviewer -- tact run 'Review the current diff and report one finding.'
herdr wait reviewer --timeout 600
herdr read reviewer --lines 500
```

`spawn` returns only after the private worker Link is ready, so an immediate
`send` cannot race the host shell. `wait` exits with the worker's exit status.
`send` submits stdin followed by Enter to a still-running worker:

```sh
printf 'continue with the tests' | herdr send reviewer
```

The projected command never accepts a Herdr pane ID, container ID, cwd, environment, Docker
option, or host command. The Herdr World targets only the pane inherited from the
host launch, remembers only panes it created, and rechecks each pane's terminal
identity before using it. The host shell `exec`s the trusted worker, so there is
no host prompt underneath it; a completed worker stays parked for inspection.
World teardown makes bounded best-effort attempts to close its panes.
`herdr read` returns only content between private worker boundaries; it does not
expose the host bootstrap command, executable path, container identity, payload,
or completion marker.

The Herdr installation and catalog contribute only one immutable bundle path.
The host resolves the granted name and supplies one generic Link shim, which
derives `herdr` from `argv[0]` and transports only
bounded argv, optional stdin, stdout, stderr, and an exit status. The protocol
has no arbitrary executable, shell string, cwd, environment, PTY, or signal API.

This proof intentionally has no Island discovery, peer communication, window
resize, arbitrary signals, arbitrary Herdr methods, persistence, or recovery
protocol. Separate same-UID host processes and their private Unix socket are
authority minimization, not a security boundary against hostile host processes.
