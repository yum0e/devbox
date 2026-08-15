from __future__ import annotations
import base64, contextlib, importlib.util, io, json, os, re, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

PATH=Path(__file__).parents[1]/"launcher"/"devc2.py"
spec=importlib.util.spec_from_file_location("devc2",PATH); D=importlib.util.module_from_spec(spec); spec.loader.exec_module(D)

def jwt(payload):
    body=base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":")).encode()).decode().rstrip("=")
    return f"header.{body}.signature"

class CliTests(unittest.TestCase):
    def test_cli(self):
        self.assertEqual(D.parse_cli(["repo"]),("start","repo",False,()))
        self.assertEqual(D.parse_cli(["run"]),("start","run",False,()))
        self.assertEqual(D.parse_cli(["run","repo","--span","herdr"]),("start","repo",False,("herdr",)))
        self.assertEqual(D.parse_cli(["repo","--span","herdr"]),("start","repo",False,("herdr",)))
        self.assertEqual(D.parse_cli(["doctor"]),("doctor",None,False,()))
        self.assertEqual(D.parse_cli(["doctor","--runtime"]),("doctor-runtime",None,False,()))
        self.assertEqual(D.parse_cli(["auth"]),("auth",None,False,()))
        self.assertEqual(D.parse_cli(["update"]),("update",None,False,()))
        self.assertEqual(D.parse_cli(["rollback"]),("rollback",None,False,()))
        self.assertEqual(D.parse_cli(["reset","repo","--yes"]),("reset","repo",True,()))
        with self.assertRaises(RuntimeError): D.parse_cli(["run","repo","--span","herdr","--span","herdr"])
        with self.assertRaises(RuntimeError): D.parse_cli(["run","repo",*sum((["--span",f"s{i}"] for i in range(17)),[])])
    def test_help_has_no_subprocess(self):
        with mock.patch.object(D.subprocess,"run") as run, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit:
            D.parse_cli(["--help"])
        self.assertEqual(exit.exception.code,0); run.assert_not_called()
    def test_identity_is_stable_and_namespaced(self):
        repo=Path("/tmp/repo")
        self.assertEqual(D.identity(repo),D.identity(repo))
        self.assertTrue(D.identity(repo)[1].startswith("devc2-"))

    def test_shared_tact_config_is_initialized_once(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "devc2" / "tact" / "config.toml"
            with mock.patch.object(D, "TACT_CONFIG", config):
                self.assertEqual(D.ensure_tact_config(), config)
                self.assertEqual(config.read_text(), "[agent]\nmax_subagents = 8\n")
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                config.write_text("[memory]\nenabled = true\n")
                D.ensure_tact_config()
                self.assertEqual(config.read_text(), "[memory]\nenabled = true\n")

    def test_shared_config_is_snapshotted_but_runtime_copy_is_writable_and_per_repo(self):
        raw = (PATH.parents[1] / "compose.yaml").read_text()
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertNotIn("source: ${DEVC2_PUBLIC_DIR:?set DEVC2_PUBLIC_DIR}/tact-config.toml", raw)
        self.assertIn("target: /run/devc2-public", raw)
        self.assertIn("shared_tact_config=/run/devc2-public/tact-config.toml", entrypoint)
        self.assertIn('install -m 0600 "$shared_tact_config" "$TACT_CONFIG"', entrypoint)
        self.assertIn("source: state-v2", raw)
        self.assertIn("target: /home/devbox", raw)

    def test_compose_hardening_and_v1_namespace(self):
        raw = (PATH.parents[1] / "compose.yaml").read_text()
        services = raw.split("services:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z][a-z0-9-]*):$", services, re.MULTILINE), ["devbox", "span-proxy"])
        self.assertNotIn("credential-proxy:", raw)
        self.assertNotIn("ssh-proxy:", raw)
        self.assertIn("mem_limit: 16g", raw)
        self.assertIn("cpus: 8", raw)
        self.assertIn("cap_drop: [ALL]", raw)
        self.assertIn("no-new-privileges:true", raw)
        self.assertNotIn("tmpfs: [/tmp:", raw)
        self.assertIn("- /tmp:size=8m,mode=1777", raw)
        devbox_section = raw.split("  devbox:", 1)[1].split("  span-proxy:", 1)[0]
        self.assertNotIn('user: "${DEVC2_UID', devbox_section)
        self.assertNotIn("docker.sock", raw)
        self.assertIn("source: span-sockets-v2", raw)
        self.assertEqual(raw.count("target: /run/devc2/spans"), 2)
        self.assertIn("target: /run/devc2-span-tls", raw)
        self.assertNotIn("/run/host-services/ssh-auth.sock", raw)
        self.assertNotIn("DEVC2_SSH_RELAY", raw)
        self.assertNotIn("upstream-secret", raw)
        self.assertNotIn("gh-config", raw)
        self.assertNotIn("GH_TOKEN:", raw)
        self.assertNotIn("claude", raw.lower())

    def test_devbox_has_technology_neutral_infra_endpoint_configuration(self):
        raw = (PATH.parents[1] / "compose.yaml").read_text()
        devbox_section = raw.split("  devbox:\n", 1)[1].split("\n  span-proxy:\n", 1)[0]
        self.assertIn("DB_HOST: ${DEVC2_DB_HOST:-postgres-primary.internal}", devbox_section)
        self.assertIn("DB_PORT: ${DEVC2_DB_PORT:-5432}", devbox_section)
        self.assertIn("DB_NAME: ${DEVC2_DB_NAME:-app}", devbox_section)
        environment = devbox_section.split("    environment:\n", 1)[1].split("    volumes:\n", 1)[0]
        for placeholder in ("TOKEN", "AUTH", "CREDENTIAL", "PROXY", "SOCK", "OPENAI", "GITHUB", "GH_"):
            self.assertNotIn(placeholder, environment.upper())
        self.assertNotIn("BOUNDARY_", devbox_section)
        self.assertNotIn("BDX_", devbox_section)

    def test_tact_wrapper_uses_openai_span_except_for_local_commands(self):
        wrapper_source = (PATH.parents[1] / "devbox" / "tact-wrapper.sh").read_text()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upstream, openai, result = root / "upstream", root / "openai", root / "result"
            upstream.write_text("#!/bin/sh\n" f"printf 'upstream:%s\\n' \"$*\" >>{result}\n" "exit 17\n")
            upstream.chmod(0o755)
            openai.write_text("#!/bin/sh\n" f"printf 'openai:%s\\n' \"$*\" >>{result}\n" "shift 2\nexec \"$@\"\n")
            openai.chmod(0o755)
            wrapper = root / "tact"
            wrapper.write_text(wrapper_source.replace("/usr/local/libexec/devc2/tact-upstream", str(upstream)).replace("/run/devc2/bin/openai", str(openai)))
            wrapper.chmod(0o755)
            for arguments in (["config", "show"], ["--version"]):
                self.assertEqual(subprocess.run([str(wrapper), *arguments]).returncode, 17)
            self.assertEqual(subprocess.run([str(wrapper), "run", "task"]).returncode, 17)
            self.assertEqual(result.read_text().splitlines(), [
                "upstream:config show", "upstream:--version",
                f"openai:run -- {upstream} run task", "upstream:run task",
            ])

    def test_external_build_images_are_content_pinned(self):
        dockerfile = (PATH.parents[1] / "Dockerfile").read_text()
        proxy_dockerfile = (PATH.parents[1] / "credential_proxy" / "Dockerfile").read_text()
        self.assertIn("docker/dockerfile:1.7@sha256:", dockerfile)
        self.assertIn("ghcr.io/clabby/tact:${TACT_VERSION}@sha256:", dockerfile)
        self.assertIn("ghcr.io/astral-sh/uv:0.9.26@sha256:", dockerfile)
        self.assertIn("python:3.12-alpine@sha256:", proxy_dockerfile)

    def test_v1_developer_tools_postgres_17_and_pnpm_managed_node(self):
        dockerfile = (PATH.parents[1] / "Dockerfile").read_text()
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        for package in (
            "bubblewrap", "fd-find", "lsof", "ncurses-term", "socat",
            "sudo", "unzip", "zoxide",
        ):
            self.assertIn(package, dockerfile)
        self.assertIn("postgresql-17 postgresql-client-17", dockerfile)
        self.assertIn("/usr/lib/postgresql/17/bin", dockerfile)
        self.assertIn("https://get.pnpm.io/install.sh", dockerfile)
        self.assertIn("${PNPM_HOME}/bin", dockerfile)
        self.assertIn("/usr/local/share/pnpm/bin", dockerfile)
        self.assertIn("pnpm runtime set node latest --global", dockerfile)
        self.assertIn("pnpm add --global @openai/codex@0.147.0", dockerfile)
        self.assertNotIn("\n && npm --version", dockerfile)
        self.assertNotIn("\n && npm install", dockerfile)
        self.assertIn("uv python install 3.14", dockerfile)
        self.assertRegex(dockerfile, r"openssh-client passwd procps")
        self.assertEqual(dockerfile.count(":/usr/sbin:"), 2)
        self.assertIn("curl --fail --location --show-error --silent https://getfoundry.sh/install", dockerfile)
        self.assertIn("head -n 1 /tmp/foundry-install.sh | grep -q '^#!'", dockerfile)
        self.assertIn("$FOUNDRY_DIR/bin/foundryup", dockerfile)
        self.assertNotIn("corepack enable", dockerfile)
        self.assertNotIn("fnm env", dockerfile)
        self.assertNotIn("nvm install", dockerfile)
        self.assertNotIn(" tmux ", dockerfile)
        self.assertNotIn("tmux -V", entrypoint)
        for probe in (
            "psql --version", "node --version",
            "pnpm --version", "python3.14 --version", "forge --version",
        ):
            self.assertIn(probe, entrypoint)

    def test_herdr_is_not_bundled_and_shell_is_the_default_environment(self):
        dockerfile = (PATH.parents[1] / "Dockerfile").read_text()
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        legacy_marker = (PATH.parents[1] / "devbox" / "herdr-wrapper.py").read_text()
        self.assertNotIn("HERDR_VERSION", dockerfile)
        self.assertNotIn("herdr-linux-", dockerfile)
        self.assertNotIn("herdr-wrapper", dockerfile)
        self.assertIn("Legacy release marker", legacy_marker)
        self.assertIn("explicitly grant a Herdr Span", legacy_marker)
        self.assertTrue(entrypoint.rstrip().endswith('exec /bin/zsh'))
        runtime_path=dockerfile.split("ENV HOME=",1)[1]
        self.assertIn(":/bin:/run/devc2/bin:/home/devbox/.local/bin",runtime_path)

    def test_runtime_doctor_is_disposable_and_exercises_live_boundaries(self):
        source = PATH.read_text()
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertIn("TemporaryDirectory(prefix='devc2-doctor-repo-')", source)
        self.assertIn("DEVC2_RUNTIME_DOCTOR=1", source)
        self.assertIn("down_args.append('--volumes')", source)
        self.assertIn('locks=STATE/"locks"', source)
        self.assertNotIn("shutil.rmtree(lock_directory", source)
        self.assertIn("runtime doctor teardown left resources", source)
        self.assertIn("ssh-add -L", entrypoint)
        self.assertIn('test "${#doctor_identities[@]}" -eq 1', entrypoint)
        self.assertNotIn("grep -c '^ssh-'", entrypoint)
        self.assertIn("openai run -- /bin/sh -ceu", entrypoint)
        self.assertIn("backend-api/codex/models?client_version=0.147.0", entrypoint)
        self.assertIn("OpenAI Span: authenticated models request", entrypoint)
        self.assertIn("github run -- gh api user", entrypoint)
        self.assertIn("tact config show", entrypoint)
        self.assertNotIn("set -x", entrypoint)

    def test_active_v1_is_refused(self):
        result = subprocess.CompletedProcess([], 0, "container-id\n", "")
        with mock.patch.object(D, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "v1 is already running"):
                D.ensure_v1_not_running(Path("/tmp/repo"))

    def test_runtime_doctor_uses_a_temporary_repo_and_checks_teardown(self):
        seen=[]
        def start(repo, runtime_doctor=False, span_names=()):
            self.assertTrue(runtime_doctor)
            self.assertEqual(span_names, ("openai", "github", "ssh-agent"))
            self.assertTrue((repo / ".git").is_dir())
            seen.append(repo)
            return 0
        empty=subprocess.CompletedProcess([],0,"","")
        with mock.patch.object(D,"require_docker"), mock.patch.object(D,"read_codex",return_value=("a","b")), mock.patch.object(D,"validate_github_token",return_value="user"), mock.patch.object(D,"public_keys",return_value=["key"]), mock.patch.object(D,"_start_unlocked",side_effect=start), mock.patch.object(D,"run",return_value=empty) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(D.doctor(runtime=True),0)
        self.assertEqual(len(seen),1)
        self.assertFalse(seen[0].exists())
        self.assertEqual(run.call_count,4)  # OpenSSL plus three teardown resource queries.

    def test_doctor_rejects_empty_agent(self):
        with mock.patch.object(D, "require_docker"), mock.patch.object(D, "read_codex", return_value=("a", "b")), mock.patch.object(D, "validate_github_token", return_value="user"), mock.patch.object(D, "public_keys", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(D.doctor(), 1)

    def test_entrypoint_uses_optional_github_and_ssh_agent_spans_with_signing_readiness(self):
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertIn("github run -- gh api user", entrypoint)
        self.assertIn("ssh-keygen -Y sign", entrypoint)
        self.assertIn('gpg.ssh.allowedSignersFile "$allowed_signers"', entrypoint)
        self.assertIn("namespaces=\"git\"", entrypoint)
        self.assertIn("SSH-agent Span selected identity could not sign", entrypoint)
        self.assertIn("if [[ -S /run/devc2/spans/ssh-agent.sock ]]", entrypoint)
        self.assertIn("git config --global --unset-all core.sshCommand", entrypoint)
        self.assertIn("jj config unset --user signing.key", entrypoint)
        self.assertLess(entrypoint.index("--unset-all core.sshCommand"),entrypoint.index("if [[ -S /run/devc2/spans/ssh-agent.sock ]]"))
        self.assertIn("if command -v github", entrypoint)
        self.assertNotIn('$HOME/.zshenv', entrypoint)
        self.assertNotIn('$HOME/.bashrc', entrypoint)
        wrapper = (PATH.parents[1] / "devbox" / "tact-wrapper.sh").read_text()
        self.assertIn('exec /run/devc2/bin/openai run -- /usr/local/libexec/devc2/tact-upstream "$@"', wrapper)
        runtime_path=(PATH.parents[1]/"Dockerfile").read_text().split("ENV HOME=",1)[1]
        self.assertLess(runtime_path.index("/usr/local/bin"),runtime_path.index("/run/devc2/bin"))
        self.assertLess(runtime_path.index("/run/devc2/bin"),runtime_path.index("/home/devbox/.local/bin"))
        self.assertIn("sanitized Span diagnostics", PATH.read_text())

class AuthTests(unittest.TestCase):
    def test_parse_cli_auth_is_a_standalone_command(self):
        with mock.patch.object(D.subprocess, "run") as run:
            self.assertEqual(D.parse_cli(["auth"]), ("auth", None, False, ()))
            with self.assertRaisesRegex(RuntimeError, "accepts no arguments"):
                D.parse_cli(["auth", "extra"])
        run.assert_not_called()

    def test_authenticate_runs_pinned_logins_in_docker(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as td:
            auth = Path(td) / "auth"
            def auth_run(argv,**_kwargs):
                if argv[:3]==["docker","image","inspect"]: return subprocess.CompletedProcess(argv,1,"","")
                return completed
            with mock.patch.object(D, "AUTH", auth), mock.patch.object(D, "require_docker"), mock.patch.object(D, "read_codex", side_effect=[RuntimeError("missing"), ("access", "account")]) as read_codex, mock.patch.object(D, "validate_github_token", side_effect=[RuntimeError("missing"), "user"]) as github_token, mock.patch.object(D, "select_key", side_effect=[RuntimeError("missing"), "ssh-key"]) as select_key, mock.patch.object(D, "run", side_effect=auth_run) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.authenticate(), 0)
            self.assertEqual(read_codex.call_count, 2)
            self.assertEqual(github_token.call_count, 2)
            self.assertEqual(select_key.call_args_list, [mock.call(False), mock.call(True)])

            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 4)
            self.assertTrue(all(command[0] in {"docker", "gh"} for command in commands))
            self.assertEqual(commands[0][:3], ["docker", "image", "inspect"])
            self.assertEqual(commands[1][:4], ["docker", "build", "--tag", "devc2:0.2.0"])
            self.assertIn(f"{auth / 'codex'}:/home/devbox/.codex", commands[2])
            self.assertIn("codex", commands[2])
            self.assertEqual(commands[2][-2:], ["login", "--device-auth"])
            self.assertIn(f"{auth / 'gh'}:/run/devc2-gh", commands[3])
            self.assertIn("gh", commands[3])
            self.assertEqual(commands[3][-7:], ["auth", "login", "--hostname", "github.com", "--git-protocol", "ssh", "--web"])
            config = auth / "codex" / "config.toml"
            self.assertEqual(config.read_text(), 'cli_auth_credentials_store = "file"\n')
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_read_codex_prefers_managed_auth(self):
        access = jwt({"exp": 4102444800})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            managed = root / "managed"
            host = root / "host"
            (managed / "codex").mkdir(parents=True)
            host.mkdir()
            (managed / "codex" / "auth.json").write_text(D.json.dumps({
                "tokens": {"access_token": access, "account_id": "managed-account"}
            }))
            (host / "auth.json").write_text("not valid json")
            with mock.patch.object(D, "AUTH", managed), mock.patch.dict(os.environ, {"CODEX_HOME": str(host)}):
                self.assertEqual(D.read_codex(), (access, "managed-account"))

    def test_github_token_uses_managed_gh_config_in_docker(self):
        completed = subprocess.CompletedProcess([], 0, "managed-token\n", "")
        with tempfile.TemporaryDirectory() as td:
            auth = Path(td) / "auth"
            gh = auth / "gh"
            gh.mkdir(parents=True)
            (gh / "hosts.yml").write_text("github.com: {}\n")
            inspected=subprocess.CompletedProcess([],0,"","")
            with mock.patch.object(D, "AUTH", auth), mock.patch.object(D, "run", side_effect=[inspected,completed]) as run:
                self.assertEqual(D.github_token(), "managed-token")
            self.assertEqual(run.call_args_list[0].args[0],["docker","image","inspect",D.AUTH_IMAGE])
            self.assertEqual(run.call_args_list[1],mock.call([
                "docker", "run", "--rm", "--pull=never", "--user", f"{os.getuid()}:{os.getgid()}",
                "--env", "HOME=/tmp", "--env", "GH_CONFIG_DIR=/run/devc2-gh",
                "--env", "GH_TOKEN=", "--env", "GITHUB_TOKEN=",
                "--volume", f"{gh}:/run/devc2-gh:ro",
                "--entrypoint", "gh", D.AUTH_IMAGE, "auth", "token",
            ], timeout=30))

    def test_bootstrapped_auth_needs_no_host_codex_or_gh(self):
        access = jwt({"exp": 4102444800})
        completed = lambda argv, stdout="": subprocess.CompletedProcess(argv, 0, stdout, "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            auth = root / "managed"
            host_codex = root / "missing-host-codex"
            commands = []

            def docker_only(argv, **_kwargs):
                commands.append(argv)
                if argv[0] == "gh":
                    raise RuntimeError("host gh unavailable")
                self.assertEqual(argv[0], "docker", f"unexpected host command: {argv}")
                if "--entrypoint" in argv and "codex" in argv:
                    (auth / "codex" / "auth.json").write_text(D.json.dumps({
                        "tokens": {"access_token": access, "account_id": "bootstrapped"}
                    }))
                if "auth" in argv and "login" in argv and "--entrypoint" in argv and "gh" in argv:
                    (auth / "gh" / "hosts.yml").write_text("github.com: {}\n")
                if argv[-2:] == ["auth", "token"]:
                    return completed(argv, "docker-gh-token\n")
                return completed(argv)

            with mock.patch.object(D, "AUTH", auth), mock.patch.object(D, "require_docker"), mock.patch.object(D, "validate_github_token", side_effect=[RuntimeError("missing"), "user"]), mock.patch.object(D, "select_key", side_effect=[RuntimeError("missing"), "ssh-key"]), mock.patch.object(D, "run", side_effect=docker_only), mock.patch.dict(os.environ, {"CODEX_HOME": str(host_codex), "PATH": ""}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.authenticate(), 0)
                self.assertEqual(D.read_codex(), (access, "bootstrapped"))
                self.assertEqual(D.github_token(), "docker-gh-token")

            self.assertTrue(commands)
            self.assertTrue(all(command[0] in {"docker", "gh"} for command in commands))


class AuthMaterialTests(unittest.TestCase):
    def test_github_token_not_logged(self):
        completed=subprocess.CompletedProcess([],0,"secret-token\n","")
        with mock.patch.object(D,"run",return_value=completed): self.assertEqual(D.github_token(),"secret-token")
    def test_key_filter_selects_saved_key(self):
        blob="AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        key=f"ssh-ed25519 {blob} selected"
        with tempfile.TemporaryDirectory() as td, mock.patch.object(D,"CONFIG",Path(td)), mock.patch.object(D,"public_keys",return_value=[key]):
            self.assertEqual(D.select_key(False),key)
            self.assertEqual(stat.S_IMODE((Path(td)/"ssh-key.pub").stat().st_mode),0o600)

class SpanTransportPKITests(unittest.TestCase):
    def test_relay_pki_is_ephemeral_scoped_and_has_expected_extensions(self):
        with tempfile.TemporaryDirectory() as td:
            server,client=D.generate_relay_pki(Path(td)/"pki")
            self.assertEqual({p.name for p in server.iterdir()},{"ca.crt","server.crt","server.key"})
            self.assertEqual({p.name for p in client.iterdir()},{"ca.crt","client.crt","client.key"})
            self.assertEqual(stat.S_IMODE((server/"server.key").stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE((client/"client.key").stat().st_mode),0o444)
            server_text=D.run(["openssl","x509","-in",str(server/"server.crt"),"-noout","-text"]).stdout
            client_text=D.run(["openssl","x509","-in",str(client/"client.crt"),"-noout","-text"]).stdout
            self.assertIn("DNS:host.docker.internal",server_text)
            self.assertIn("TLS Web Server Authentication",server_text)
            self.assertIn("TLS Web Client Authentication",client_text)

class ComposeEnvironmentTests(unittest.TestCase):
    def test_compose_env_projects_only_explicit_span_paths_without_ambient_auth(self):
        ambient = {
            "SSH_AUTH_SOCK": "/tmp/ambient-agent.sock", "GH_TOKEN": "github-secret",
            "GITHUB_TOKEN": "github-secret-2", "OPENAI_API_KEY": "openai-secret",
            "CODEX_HOME": "/tmp/codex", "HTTP_PROXY": "http://proxy.invalid",
        }
        with mock.patch.dict(os.environ, ambient, clear=False):
            result=D.compose_env(
                Path("/tmp/repo"), Path("/tmp/public"),
                Path("/tmp/span-tls"), Path("/tmp/span-clients"), True,
            )
        self.assertEqual(result["DEVC2_SPAN_RELAY_TLS_DIR"], "/tmp/span-tls")
        self.assertEqual(result["DEVC2_SPAN_CLIENT_DIR"], "/tmp/span-clients")
        self.assertRegex(result["DEVC2_SPAN_VOLUME"], r"^devc2-[0-9a-f]{16}-span-sockets-v2$")
        self.assertEqual(result["COMPOSE_PROFILES"], "spans")
        for name in ambient:
            self.assertNotIn(name, result)

    def test_span_socket_volume_removal_is_exact_and_verified(self):
        removed=subprocess.CompletedProcess([],0,"","")
        absent=subprocess.CompletedProcess([],1,"","not found")
        name="devc2-0123456789abcdef-span-sockets-v2"
        with mock.patch.object(D,"run",side_effect=[removed,absent]) as run:
            D.remove_span_volume(name)
        self.assertEqual(run.call_args_list[0].args[0],["docker","volume","rm",name])
        self.assertEqual(run.call_args_list[1].args[0],["docker","volume","inspect",name])
        with self.assertRaisesRegex(RuntimeError,"could not remove"), mock.patch.object(D,"run",side_effect=[removed,removed]):
            D.remove_span_volume(name)

class CoexistenceTests(unittest.TestCase):
    def test_reset_project_is_v2_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td); calls=[]
            state=repo/"state"
            with mock.patch.object(D,"compose",side_effect=lambda *a,**k: calls.append(a)), mock.patch.object(D,"STATE",state), mock.patch.object(D,"INSTALL_LOCK",state/"install.lock"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.reset(repo,True),0)
            self.assertTrue(D.identity(repo)[1].startswith("devc2-")); self.assertIn("down", calls[0])
    def test_source_contains_no_devcontainer_mutation(self):
        source=PATH.read_text()
        self.assertNotIn('mkdir(".devcontainer',source)
        self.assertNotIn('rmtree(".devcontainer',source)

if __name__=="__main__": unittest.main()
