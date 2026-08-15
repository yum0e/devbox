# Probe Span

This deliberately small reference provider validates the Span boundary. It can
report its launch identity and echo at most 1 MiB of opaque input. It cannot run
commands or read arbitrary host files.

Install it from a host shell:

```sh
./v2/examples/probe-span/install.sh
cat "${XDG_CONFIG_HOME:-$HOME/.config}/devc2/spans.json"
devc2 run . --span probe
```

Then, inside the granted Island:

```sh
probe info
printf 'opaque span bytes\n' | probe echo
```

In another host shell, `pgrep -fl 'devc2-spans/probe/.*/provider'` shows the scoped provider.
Exit the Island and repeat that command to confirm the provider was reaped.

Launching `devc2 run .` without `--span probe` projects neither the client nor
its socket, even when the provider is installed on the host.
