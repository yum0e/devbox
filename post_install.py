#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
from pathlib import Path

TMUX_CONFIG = """\
set -g default-terminal "tmux-256color"
set -g focus-events on
set -g extended-keys on
set -g extended-keys-format csi-u
set -sg escape-time 10
set -g mouse on
set -g history-limit 200000
set -g renumber-windows on
setw -g mode-keys vi

# Keep new panes/windows in the same cwd
bind c new-window -c "#{pane_current_path}"
bind | split-window -h -c "#{pane_current_path}"
bind - split-window -v -c "#{pane_current_path}"
unbind '"'
unbind %

# Reload config
bind r source-file ~/.tmux.conf \\; display-message "tmux.conf reloaded"

# Terminal features
set -as terminal-features ",xterm-ghostty:RGB"
set -as terminal-features ",xterm*:RGB"
set -ga terminal-overrides ",xterm*:colors=256"
set -ga terminal-overrides '*:Ss=\\E[%p1%d q:Se=\\E[ q'
"""


def log(message: str) -> None:
    print(f"post-install: {message}", file=sys.stderr)


def run_git(
    args: list[str], cwd: Path, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def run_sudo(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def resolve_workspace() -> Path:
    env_workspace = os.environ.get("WORKSPACE_FOLDER")
    if env_workspace:
        workspace = Path(env_workspace)
    else:
        workspace = Path("/workspace")
    if workspace.exists():
        return workspace
    return Path.cwd()


def is_git_repo(cwd: Path) -> bool:
    result = run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_global_gitignore(workspace: Path) -> None:
    result = run_git(["config", "--global", "--path", "core.excludesfile"], workspace)
    if result.returncode != 0:
        log("no global core.excludesfile configured")
        return

    raw_path = result.stdout.strip()
    if not raw_path:
        log("no global core.excludesfile configured")
        return

    excludes_path = Path(raw_path).expanduser()
    if not excludes_path.is_absolute():
        excludes_path = (Path.home() / excludes_path).resolve()

    if excludes_path.exists():
        log(f"global core.excludesfile exists at {excludes_path}")
        return

    source = workspace / ".devcontainer" / ".gitignore_global"
    if not source.exists():
        log(
            f"global core.excludesfile missing at {excludes_path} and no template copy found"
        )
        return

    excludes_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, excludes_path)
    log(f"copied gitignore to {excludes_path}")


def ensure_codex_config() -> None:
    codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    codex_dir.mkdir(parents=True, exist_ok=True)
    codex_config = codex_dir / "config.toml"
    if codex_config.exists():
        log(f"skipping codex config (already exists at {codex_config})")
        return

    codex_config.write_text(
        'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n',
        encoding="utf-8",
    )
    log(f"wrote default codex config to {codex_config}")


def ensure_claude_config() -> None:
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_config = claude_dir / "settings.json"
    if claude_config.exists():
        log(f"skipping claude settings (already exists at {claude_config})")
        return

    data = {"permissions": {"defaultMode": "bypassPermissions"}}
    claude_config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log(f"wrote default claude settings to {claude_config}")


def ensure_pi_config() -> None:
    pi_agent_dir = Path(
        os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
    )
    pi_agent_dir.mkdir(parents=True, exist_ok=True)
    pi_config = pi_agent_dir / "settings.json"
    if pi_config.exists():
        log(f"skipping pi settings (already exists at {pi_config})")
        return

    data = {"shellPath": "/usr/bin/zsh"}
    pi_config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log(f"wrote default pi settings to {pi_config}")


def ensure_jj_config() -> None:
    jj_config = Path(
        os.environ.get(
            "JJ_CONFIG",
            str(
                Path(
                    os.environ.get(
                        "XDG_CONFIG_HOME",
                        str(Path.home() / ".config"),
                    )
                )
                / "jj"
                / "config.toml"
            ),
        )
    ).expanduser()
    jj_config.parent.mkdir(parents=True, exist_ok=True)
    if not jj_config.exists():
        jj_config.write_text("# jj user config\n", encoding="utf-8")
        log(f"wrote default jj config to {jj_config}")
    else:
        log(f"skipping jj config (already exists at {jj_config})")

    legacy_jj_config = Path.home() / ".jjconfig.toml"
    if legacy_jj_config.is_symlink():
        if legacy_jj_config.resolve() == jj_config.resolve():
            return
        legacy_jj_config.unlink()
        legacy_jj_config.symlink_to(jj_config)
        log(f"updated jj legacy config symlink at {legacy_jj_config}")
        return

    if legacy_jj_config.exists():
        log(
            f"leaving existing legacy jj config at {legacy_jj_config}; "
            f"canonical config path is {jj_config}"
        )
        return

    legacy_jj_config.symlink_to(jj_config)
    log(f"linked legacy jj config path to {jj_config}")


def ensure_zsh_config() -> None:
    zsh_config_dir = (
        Path(
            os.environ.get(
                "XDG_CONFIG_HOME",
                str(Path.home() / ".config"),
            )
        )
        / "zsh"
    )
    zsh_config_dir.mkdir(parents=True, exist_ok=True)
    zsh_config = zsh_config_dir / "config.zsh"
    if zsh_config.exists():
        existing = zsh_config.read_text(encoding="utf-8")
        if existing.lstrip().startswith("# default zsh config for the devcontainer"):
            zsh_config.write_text(ZSH_CONFIG, encoding="utf-8")
            log(f"updated default zsh config at {zsh_config}")
            return
        log(f"skipping zsh config (already exists at {zsh_config})")
        return

def ensure_zsh_history() -> None:
    history_volume = Path("/commandhistory")
    history_volume.mkdir(parents=True, exist_ok=True)
    target = history_volume / ".zsh_history"

    zsh_history = Path.home() / ".local" / "share" / "zsh" / "zsh_history"
    zsh_history.parent.mkdir(parents=True, exist_ok=True)

    if zsh_history.is_symlink():
        if zsh_history.resolve() == target:
            return
        zsh_history.unlink()
        zsh_history.symlink_to(target)
        log(f"updated zsh history symlink at {zsh_history}")
        return

    if zsh_history.exists():   
        if not target.exists():
            zsh_history.replace(target)
            log(f"moved zsh history to {target}")
        else:
            log(f"existing zsh history left at {zsh_history}")
            return

    zsh_history.symlink_to(target)
    log(f"linked zsh history to {target}")


def ensure_dir_ownership(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        stat = path.stat()
    except OSError as exc:
        log(f"unable to stat {path}: {exc}")
        return

    uid = os.getuid()
    gid = os.getgid()
    if stat.st_uid == uid and stat.st_gid == gid:
        return

    result = run_sudo(["chown", "-R", f"{uid}:{gid}", str(path)])
    if result.returncode != 0:
        log(f"failed to chown {path}: {result.stderr.strip()}")
        return
    log(f"fixed ownership for {path}")


def install_tmux_config() -> None:
    tmux_dest = Path.home() / ".tmux.conf"
    if tmux_dest.exists():
        log(f"skipping tmux config (already exists at {tmux_dest})")
        return

    tmux_dest.write_text(TMUX_CONFIG, encoding="utf-8")
    log(f"installed tmux config to {tmux_dest}")


def check_jj_available() -> None:
    jj_path = shutil.which("jj")
    if not jj_path:
        log("warning: jj not found on PATH")
        return

    result = subprocess.run([jj_path, "--version"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unknown error"
        log(f"warning: jj found at {jj_path} but failed: {error}")
        return

    log(f"jj available: {result.stdout.strip()}")


def main() -> None:
    workspace = resolve_workspace()
    if not is_git_repo(workspace):
        log(f"skipping git repo checks (no repo at {workspace})")

    install_tmux_config()
    ensure_dir_ownership(Path("/commandhistory"))
    ensure_dir_ownership(Path.home() / ".claude")
    ensure_dir_ownership(Path.home() / ".codex")
    ensure_dir_ownership(Path.home() / ".pi")
    ensure_dir_ownership(Path.home() / ".config" / "gh")
    ensure_dir_ownership(Path.home() / ".config" / "jj")
    ensure_zsh_history()
    ensure_global_gitignore(workspace)
    ensure_codex_config()
    ensure_claude_config()
    ensure_pi_config()
    ensure_jj_config()
    ensure_zsh_config()
    check_jj_available()
    log("configured defaults for container use")


if __name__ == "__main__":
    main()
