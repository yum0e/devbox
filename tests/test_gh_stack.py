from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
CONFIGURATOR = ROOT / "box" / "configure-gh-stack.sh"
ENTRYPOINT = ROOT / "box" / "entrypoint.sh"


class GhStackTests(unittest.TestCase):
    def run_configurator(self, home: Path, managed: Path, gh_config: Path | None = None):
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "DEVC2_GH_STACK_BINARY": str(managed),
            "GH_CONFIG_DIR": str(gh_config or home / "alternate-gh-config"),
        })
        subprocess.run(["sh", str(CONFIGURATOR)], check=True, env=environment)

    def test_dockerfile_pins_official_architecture_assets_and_seeds_extension(self):
        source = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("ARG GH_STACK_VERSION=0.1.0", source)
        self.assertIn("releases/download/${gh_stack_tag}/linux-${TARGETARCH}", source)
        self.assertIn("358552dd7dce0a46ce153fe196270cec482b84f080947890aad4061a8d44bc0b", source)
        self.assertIn("a79649e121845b7404109de21d65601c09c8c6d021d93738a3428d23986a8841", source)
        self.assertIn("/usr/local/libexec/devc2/gh-stack", source)
        self.assertIn("/home/devbox/.local/share/gh/extensions/gh-stack/gh-stack", source)

    def test_entrypoint_runs_configurator(self):
        self.assertIn(
            "/usr/local/libexec/devc2/configure-gh-stack",
            ENTRYPOINT.read_text(encoding="utf-8"),
        )

    def test_fresh_home_uses_standard_extension_storage_not_gh_config_dir(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            home.mkdir()
            managed = root / "managed-gh-stack"
            managed.write_text("#!/bin/sh\n", encoding="utf-8")
            managed.chmod(0o755)
            alternate = root / "config"

            self.run_configurator(home, managed, alternate)

            extension = home / ".local/share/gh/extensions/gh-stack"
            self.assertTrue(extension.is_dir())
            self.assertEqual(os.readlink(extension / "gh-stack"), str(managed))
            self.assertFalse(alternate.exists())

    def test_wrong_managed_symlink_is_repaired_but_real_binary_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            managed = root / "managed-gh-stack"
            managed.write_text("#!/bin/sh\n", encoding="utf-8")
            managed.chmod(0o755)
            home = root / "home"
            extension = home / ".local/share/gh/extensions/gh-stack"
            extension.mkdir(parents=True)
            binary = extension / "gh-stack"
            binary.symlink_to(root / "old-managed-binary")

            self.run_configurator(home, managed)
            self.assertEqual(os.readlink(binary), str(managed))

            binary.unlink()
            binary.write_text("user managed\n", encoding="utf-8")
            self.run_configurator(home, managed)
            self.assertEqual(binary.read_text(encoding="utf-8"), "user managed\n")
            self.assertFalse(binary.is_symlink())

    def test_user_managed_extension_directory_symlink_is_untouched(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            managed = root / "managed-gh-stack"
            managed.write_text("#!/bin/sh\n", encoding="utf-8")
            managed.chmod(0o755)
            home = root / "home"
            extensions = home / ".local/share/gh/extensions"
            extensions.mkdir(parents=True)
            user_directory = root / "user-extension"
            user_directory.mkdir()
            extension = extensions / "gh-stack"
            extension.symlink_to(user_directory, target_is_directory=True)

            self.run_configurator(home, managed)

            self.assertTrue(extension.is_symlink())
            self.assertEqual(os.readlink(extension), str(user_directory))
            self.assertEqual(list(user_directory.iterdir()), [])

    def test_user_managed_parent_symlink_is_untouched(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            managed = root / "managed-gh-stack"
            managed.write_text("#!/bin/sh\n", encoding="utf-8")
            managed.chmod(0o755)
            home = root / "home"
            home.mkdir()
            redirected = root / "redirected"
            redirected.mkdir()
            (home / ".local").symlink_to(redirected, target_is_directory=True)

            self.run_configurator(home, managed)

            self.assertEqual(list(redirected.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
