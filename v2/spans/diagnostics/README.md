# Diagnostics World

`diagnostics` is an optional, read-only command World for one devc2 launch. It
reports sanitized transport state for the Spans explicitly granted to that
Island:

```sh
devc2 run . --span diagnostics --span ssh-agent
# inside the Island
diagnostics report
```

The report contains names, lifecycle stages, bounded counters, and exception
class names. It contains no host paths, process environment, credentials,
traffic contents, Docker socket, arbitrary file access, or command execution.
The World receives one private state file created for that launch; other Worlds
do not receive its path. It deliberately has no mutation or generic probe
command.
