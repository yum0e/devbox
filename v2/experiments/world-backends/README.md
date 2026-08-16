# Backend substitution proof

This experiment asks one question: can an unchanged graph of Worlds and Links
survive a change in how its Worlds are launched?

```text
caller ── projected `herdr` ──▶ Herdr World
                                     │
                              projected `agent-worker`
                                     │
                                     ▼
                                Agent World
```

Run it inside the Island:

```sh
python3 v2/experiments/world-backends/proof.py
```

The same logical graph runs once with direct processes and once with a local
backend that stages identical World artifacts into private runtime directories
and relays their Links. Both use the production generic command projection and
produce the same observed result.
The proof also checks stop-before-readiness, stop idempotence, shutdown with a
partial active Link, failed-launch rollback, route removal, process reaping,
relay joins, and endpoint cleanup.

The backend surface under test is a preconfigured closure:

```text
launch() -> stop()
```

Backend-specific definitions never cross the graph runner: it receives only
preconfigured launch closures. The returned closure requests shutdown. Readiness
is a successful bounded exchange with the Link; command semantics belong to
Worlds; communication happens through Links. The relayed backend is not claimed
to be a stronger security boundary. It proves that artifact placement and Link
transport can remain backend-private, but is intentionally local because the
current Island prohibits creating another namespace.

The important negative result is equally explicit: Herdr must not receive a
backend `exec` method. An Agent World instead exports the narrow `agent-worker`
affordance. Proving that a Docker, VM, or SSH backend can preserve the same
logical graph—including artifact placement and Link transport—is the next,
stronger proof; this experiment makes no same-UID isolation or security claim.
