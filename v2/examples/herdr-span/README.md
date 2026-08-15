# Herdr Span

This experimental Span lets one Island create host-visible Herdr panes without
giving the Island Herdr's host socket or Docker. Every spawned command still
runs inside the same Island.

```text
                         HOST

 ┌─ Herdr session ────────────────────────────────────────────┐
 │                                                           │
 │  pane: devc2                       pane: reviewer           │
 │  ┌──────────────────────┐          ┌──────────────────────┐ │
 │  │ Island shell         │  split   │ trusted worker       │ │
 │  │                      │ ───────► │                      │ │
 │  │ $ herdr spawn ... ───┼──bytes──►│ docker exec          │ │
 │  └──────────┬───────────┘          └──────────┬───────────┘ │
 │             │                                 │             │
 └─────────────┼─────────────────────────────────┼─────────────┘
               │                                 │
 ══════════════╪══════════ ISLAND BOUNDARY ══════╪══════════════
               │                                 │
       projected `herdr`                 command + argv
               │                                 │
       ┌───────▼─────────────────────────────────▼───────┐
       │                    ISLAND                       │
       │                                                │
       │  repo · shell · tact · tests · spawned process │
       │                                                │
       │  no Docker socket · no host Herdr socket       │
       └────────────────────────────────────────────────┘

 grant: provider + client + opaque byte stream
 deny:  host command · host pane ID · Docker option · host socket
```

The pattern is intentionally small: the provider performs five fixed operations
and Herdr remains unaware of Spans or Islands. As in a narrow control-plane
agent, adding more orchestration later should be a loop over these primitives,
not a reason to widen the boundary.

## Install and run

Herdr must already be running on the host. From a host Herdr pane:

```sh
./v2/examples/herdr-span/install.sh
devc2 run . --span herdr
```

The provider deliberately refuses to start if `devc2` was not launched from a
Herdr-managed pane.

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

`wait` exits with the worker's exit status. `send` submits stdin followed by
Enter to a still-running worker:

```sh
printf 'continue with the tests' | herdr send reviewer
```

The client never accepts a Herdr pane ID, container ID, cwd, environment, Docker
option, or host command. The provider targets only the pane inherited from the
host launch, remembers only panes it created, and rechecks each pane's terminal
identity before using it. The host shell `exec`s the trusted worker, so there is
no host prompt underneath it; a completed worker stays parked for inspection.
Provider teardown makes bounded best-effort attempts to close its panes.

This first version intentionally has no Island discovery, peer communication,
raw terminal keys, arbitrary Herdr methods, persistence, or recovery protocol.
