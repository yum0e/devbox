from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXECUTABLES = {
    "install.sh", "devbox/entrypoint.sh", "devbox/configure-pi.sh",
    "devbox/configure-signing.sh",
    "devbox/configure-herdr.sh",
    "devbox/configure-gh-stack.sh",
    "devbox/herdr-git-metadata",
    "launcher/devc2.py", "launcher/dispatcher.py",
    "launcher/command_projection.py", "spans/http-credential-world",
    "spans/ssh-agent/world", "spans/diagnostics/world",
}


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PACKAGER = load("devc2_release_packager", ROOT / "package-release.py")
LAUNCHER = load("devc2_packaged_launcher", ROOT / "launcher" / "devc2.py")


class PackagedImageTests(unittest.TestCase):
    def build_package(self, root: Path) -> tuple[Path, Path]:
        PACKAGER.DIST = root / "dist"
        with contextlib.redirect_stdout(io.StringIO()):
            PACKAGER.main()
        archive = PACKAGER.DIST / "devc2.tar.gz"
        extracted = root / "extracted"
        package_root = LAUNCHER.extract_release(archive, extracted)
        LAUNCHER.validate_asset_tree(package_root, True)
        return archive, package_root

    def test_release_archive_is_deterministic_complete_and_buildable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, package_root = self.build_package(root)
            first = archive.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                PACKAGER.main()
            self.assertEqual(archive.read_bytes(), first)

            with tarfile.open(archive, "r:gz") as package:
                members = {member.name: member for member in package.getmembers()}
            self.assertIn("devc2/spans/http-credential-world", members)
            self.assertNotIn("devc2/spans/openai/world", members)
            self.assertNotIn("devc2/spans/github/world", members)
            self.assertEqual(PACKAGER.EXECUTABLE_ASSETS, EXPECTED_EXECUTABLES)
            for relative in EXPECTED_EXECUTABLES:
                member = members["devc2/" + relative]
                self.assertTrue(member.isfile(), relative)
                self.assertEqual(stat.S_IMODE(member.mode), 0o755, relative)
            for relative in ("Dockerfile", "compose.yaml", "credential_proxy/span_bridge.py"):
                self.assertEqual(stat.S_IMODE(members["devc2/" + relative].mode), 0o644,
                                 relative)
            for relative in ("devc2", "devc2/spans", "devc2/credential_proxy"):
                self.assertTrue(members[relative].isdir(), relative)
                self.assertEqual(stat.S_IMODE(members[relative].mode), 0o755, relative)

            for dockerfile in (package_root / "Dockerfile", package_root / "credential_proxy" / "Dockerfile"):
                source = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")
                for line in source.splitlines():
                    if not line.lstrip().upper().startswith("COPY "):
                        continue
                    fields = shlex.split(line)
                    if any(field.startswith("--from=") for field in fields[1:]):
                        continue
                    local = [field for field in fields[1:-1] if not field.startswith("--")]
                    for relative in local:
                        self.assertTrue((package_root / relative).exists(),
                                        f"{dockerfile.relative_to(package_root)} requires {relative}")

    def test_packager_rejects_an_incomplete_runtime_before_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); previous_root=PACKAGER.ROOT; previous_dist=PACKAGER.DIST
            PACKAGER.ROOT=root; PACKAGER.DIST=root/"dist"
            try:
                with self.assertRaisesRegex(RuntimeError,"lacks required asset"):
                    PACKAGER.main()
                self.assertFalse(PACKAGER.DIST.exists())
            finally:
                PACKAGER.ROOT=previous_root; PACKAGER.DIST=previous_dist

    @unittest.skipUnless(os.environ.get("DEVC2_TEST_PACKAGED_IMAGES") == "1",
                         "set DEVC2_TEST_PACKAGED_IMAGES=1 on a Docker host")
    def test_docker_builds_and_smokes_packaged_images(self):
        docker = shutil.which("docker")
        if docker is None:
            self.fail("DEVC2_TEST_PACKAGED_IMAGES=1 requires Docker")
        subprocess.run([docker, "info"], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _archive, package_root = self.build_package(root)
            digest = LAUNCHER.asset_digest(package_root)
            suffix = f"{secrets.token_hex(6)}-{digest[:12]}"
            tags = {
                "runtime": f"devc2-packaged-runtime-test:{suffix}",
                "auth": f"devc2-packaged-auth-test:{suffix}",
                "proxy": f"devc2-packaged-proxy-test:{suffix}",
            }
            containers = {
                name: f"devc2-packaged-{name}-test-{suffix}"
                for name in tags
            }
            label = f"{LAUNCHER.IMAGE_LABEL}={digest}"
            try:
                for target in ("runtime", "auth"):
                    subprocess.run([docker, "build", "--pull", "--target", target,
                                    "--label", label, "--tag", tags[target], str(package_root)],
                                   check=True, timeout=3600)
                subprocess.run([docker, "build", "--pull", "--file",
                                str(package_root / "credential_proxy" / "Dockerfile"),
                                "--label", label, "--tag", tags["proxy"], str(package_root)],
                               check=True, timeout=1800)

                for tag in tags.values():
                    inspected = subprocess.run(
                        [docker, "image", "inspect", "--format",
                         '{{json .Config.Labels}}', tag], check=True, text=True,
                        capture_output=True, timeout=30)
                    self.assertEqual(json.loads(inspected.stdout)[LAUNCHER.IMAGE_LABEL], digest)
                subprocess.run([docker, "run", "--rm", "--name", containers["runtime"],
                                "--pull=never", "--entrypoint", "/bin/sh",
                                tags["runtime"], "-ec",
                                "command -v tact; command -v pi; command -v herdr; command -v gh; "
                                "test \"$(herdr --version)\" = 'herdr 0.8.2'; "
                                "test \"$(stat -c '%u:%g:%a' /home/devbox/.config)\" = '1000:1000:700'; "
                                "test \"$(stat -c '%u:%g:%a' /home/devbox/.config/herdr)\" = '1000:1000:700'; "
                                "test -x /usr/local/bin/devc2-entrypoint"],
                               check=True, timeout=60)
                subprocess.run([docker, "run", "--rm", "--name", containers["auth"],
                                "--pull=never", "--entrypoint", "codex",
                                tags["auth"], "--version"], check=True, timeout=60)
                subprocess.run([docker, "run", "--rm", "--name", containers["proxy"],
                                "--pull=never", "--entrypoint", "python",
                                tags["proxy"], "-c",
                                "import credential_proxy.span_bridge,credential_proxy.http_projection"],
                               check=True, timeout=60)
            finally:
                for name in reversed(tuple(containers.values())):
                    subprocess.run([docker, "container", "rm", "--force", name], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                for tag in reversed(tuple(tags.values())):
                    subprocess.run([docker, "image", "rm", "--force", tag], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


if __name__ == "__main__":
    unittest.main()
