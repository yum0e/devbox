"""Single source of truth for immutable devc2 runtime assets."""

ASSET_ROOTS = (
    "Dockerfile",
    "compose.yaml",
    "README.md",
    "install.sh",
    "devbox",
    "credential_proxy",
    "launcher",
    "spans",
)

REQUIRED_ASSETS = (
    "Dockerfile",
    "compose.yaml",
    "devbox/entrypoint.sh",
    "devbox/configure-pi.sh",
    "devbox/configure-signing.sh",
    "devbox/configure-herdr.sh",
    "devbox/configure-gh-stack.sh",
    "devbox/agent-skills/herdr/SKILL.md",
    "devbox/github-known-hosts",
    "devbox/herdr-config.toml",
    "devbox/herdr-git-metadata",
    "devbox/herdr-zsh-integration.zsh",
    "launcher/asset_contract.py",
    "launcher/devc2.py",
    "launcher/span_runtime.py",
    "launcher/span_supervisor.py",
    "launcher/dispatcher.py",
    "launcher/command_projection.py",
    "launcher/world_attachment.py",
    "credential_proxy/Dockerfile",
    "credential_proxy/__init__.py",
    "credential_proxy/config.py",
    "credential_proxy/span_bridge.py",
    "credential_proxy/stream_relay.py",
    "credential_proxy/http_projection.py",
    "spans/http-credential-world",
    "spans/ssh-agent/world",
    "spans/diagnostics/world",
)

EXECUTABLE_ASSETS = frozenset((
    "install.sh",
    "devbox/entrypoint.sh",
    "devbox/configure-pi.sh",
    "devbox/configure-signing.sh",
    "devbox/configure-herdr.sh",
    "devbox/configure-gh-stack.sh",
    "devbox/herdr-git-metadata",
    "launcher/devc2.py",
    "launcher/dispatcher.py",
    "launcher/command_projection.py",
    "spans/http-credential-world",
    "spans/ssh-agent/world",
    "spans/diagnostics/world",
))


def validate_required_assets(root):
    for relative in REQUIRED_ASSETS:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"release lacks required asset: {relative}")
