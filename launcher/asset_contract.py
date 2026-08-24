"""Single source of truth for immutable devc2 runtime assets."""

ASSET_ROOTS = (
    "Dockerfile",
    "compose.yaml",
    "README.md",
    "install.sh",
    "box",
    "span_gateway",
    "launcher",
    "spans",
)

REQUIRED_ASSETS = (
    "Dockerfile",
    "compose.yaml",
    "box/entrypoint.sh",
    "box/configure-pi.sh",
    "box/configure-signing.sh",
    "box/configure-herdr.sh",
    "box/configure-gh-stack.sh",
    "box/agent-skills/herdr/SKILL.md",
    "box/github-known-hosts",
    "box/herdr-config.toml",
    "box/herdr-git-metadata",
    "box/herdr-zsh-integration.zsh",
    "launcher/asset_contract.py",
    "launcher/devc2.py",
    "launcher/span_runtime.py",
    "launcher/span_supervisor.py",
    "launcher/dispatcher.py",
    "launcher/command_adapter.py",
    "launcher/http_attachment.py",
    "span_gateway/Dockerfile",
    "span_gateway/__init__.py",
    "span_gateway/config.py",
    "span_gateway/gateway.py",
    "span_gateway/stream_relay.py",
    "span_gateway/http_connect_proxy.py",
    "spans/http-credential-world",
    "spans/ssh-agent/world",
    "spans/diagnostics/world",
)

EXECUTABLE_ASSETS = frozenset((
    "install.sh",
    "box/entrypoint.sh",
    "box/configure-pi.sh",
    "box/configure-signing.sh",
    "box/configure-herdr.sh",
    "box/configure-gh-stack.sh",
    "box/herdr-git-metadata",
    "launcher/devc2.py",
    "launcher/dispatcher.py",
    "launcher/command_adapter.py",
    "spans/http-credential-world",
    "spans/ssh-agent/world",
    "spans/diagnostics/world",
))


def validate_required_assets(root):
    for relative in REQUIRED_ASSETS:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"release lacks required asset: {relative}")
