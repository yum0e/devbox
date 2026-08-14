from __future__ import annotations
import contextlib, importlib.util, io, os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

PATH=Path(__file__).parents[1]/"launcher"/"devc2.py"
spec=importlib.util.spec_from_file_location("devc2",PATH); D=importlib.util.module_from_spec(spec); spec.loader.exec_module(D)

class CliTests(unittest.TestCase):
    def test_cli(self):
        self.assertEqual(D.parse_cli(["repo"]),("start","repo",False))
        self.assertEqual(D.parse_cli(["doctor"]),("doctor",None,False))
        self.assertEqual(D.parse_cli(["doctor","--runtime"]),("doctor-runtime",None,False))
        self.assertEqual(D.parse_cli(["auth"]),("auth",None,False))
        self.assertEqual(D.parse_cli(["update"]),("update",None,False))
        self.assertEqual(D.parse_cli(["rollback"]),("rollback",None,False))
        self.assertEqual(D.parse_cli(["reset","repo","--yes"]),("reset","repo",True))
        with self.assertRaises(RuntimeError): D.parse_cli(["a","b"])
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
        self.assertIn("mem_limit: 16g", raw)
        self.assertIn("cpus: 8", raw)
        self.assertIn("cap_drop: [ALL]", raw)
        self.assertIn("no-new-privileges:true", raw)
        self.assertNotIn("tmpfs: [/tmp:", raw)
        self.assertIn("- /tmp:size=16m,mode=1777", raw)
        self.assertIn("- /tmp:size=8m,mode=1777", raw)
        devbox_section = raw.split("  devbox:", 1)[1].split("  credential-proxy:", 1)[0]
        self.assertNotIn('user: "${DEVC2_UID', devbox_section)
        ssh_proxy_section = raw.rsplit("  ssh-proxy:", 1)[1]
        self.assertIn('user: "0:0"', ssh_proxy_section)
        self.assertIn("--drop-uid", ssh_proxy_section)
        self.assertIn("--drop-gid", ssh_proxy_section)
        self.assertIn("cap_add: [SETUID, SETGID]", ssh_proxy_section)
        self.assertNotIn("ssh-socket-init", raw)
        self.assertNotIn("docker.sock", raw)
        self.assertIn("source: ssh-socket-v2", raw)
        self.assertNotIn("source: ssh-socket\n", raw)
        self.assertNotIn("source: /run/host-services/ssh-auth.sock", raw)
        self.assertNotIn("DEVC2_HOST_AGENT_SOCKET", raw)
        self.assertIn("host.docker.internal:${DEVC2_SSH_RELAY_PORT", raw)
        self.assertIn("target: /run/devc2-ssh-tls", raw)
        self.assertEqual(raw.count("target: /run/devc2-ssh-tls"), 1)
        self.assertNotIn("devc2-ssh-tls", devbox_section)
        self.assertIn("--tls-server-name", raw)
        self.assertNotIn("upstream-secret", raw)
        self.assertIn("SSH relay credentials were exposed inside the devbox", (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text())
        self.assertNotIn("gh-config", raw)
        self.assertIn("GH_CONFIG_DIR: /run/devc2-public/gh", raw)
        self.assertNotIn("GH_TOKEN:", raw)
        self.assertNotIn("claude", raw.lower())

    def test_tact_wrapper_restores_socket_and_forwards_arguments(self):
        wrapper_source = (PATH.parents[1] / "devbox" / "tact-wrapper.sh").read_text()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upstream = root / "upstream"
            result = root / "result"
            upstream.write_text(
                "#!/bin/sh\n"
                f"printf '%s\n' \"$SSH_AUTH_SOCK\" \"$@\" >{result}\n"
                "exit 17\n"
            )
            upstream.chmod(0o755)
            wrapper = root / "tact"
            wrapper.write_text(wrapper_source.replace("/usr/local/libexec/devc2/tact-upstream", str(upstream)))
            wrapper.chmod(0o755)
            completed = subprocess.run([str(wrapper), "config", "show"], env={**os.environ, "SSH_AUTH_SOCK": "/wrong"})
            self.assertEqual(completed.returncode, 17)
            self.assertEqual(result.read_text().splitlines(), ["/run/devc2-ssh/agent.sock", "config", "show"])

    def test_herdr_wrapper_normalizes_only_clean_server_shutdown(self):
        source=(PATH.parents[1]/"devbox"/"herdr-wrapper.py").read_text()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); upstream=root/"herdr-upstream"; wrapper=root/"herdr"; wrapper.write_text(source.replace("/usr/local/libexec/devc2/herdr-upstream",str(upstream)))
            cases=(("herdr: server shut down: server is shutting down",1,0),("herdr: unexpected crash",1,1),("herdr: server shut down: server is shutting down",2,2))
            for message,status,expected in cases:
                upstream.write_text(f"#!/bin/sh\nprintf '%s\n' '{message}' >&2\nexit {status}\n"); upstream.chmod(0o755)
                result=subprocess.run([sys.executable,str(wrapper)],text=True,capture_output=True)
                self.assertEqual(result.returncode,expected,result.stderr)
                self.assertIn(message,result.stderr)
        dockerfile=(PATH.parents[1]/"Dockerfile").read_text()
        self.assertIn("/usr/local/libexec/devc2/herdr-upstream",dockerfile)
        self.assertIn("COPY devbox/herdr-wrapper.py /usr/local/bin/herdr",dockerfile)

    def test_external_build_images_are_content_pinned(self):
        dockerfile = (PATH.parents[1] / "Dockerfile").read_text()
        proxy_dockerfile = (PATH.parents[1] / "credential_proxy" / "Dockerfile").read_text()
        self.assertIn("docker/dockerfile:1.7@sha256:", dockerfile)
        self.assertIn("ghcr.io/clabby/tact:${TACT_VERSION}@sha256:", dockerfile)
        self.assertIn("ghcr.io/astral-sh/uv:0.9.26@sha256:", dockerfile)
        self.assertIn("python:3.12-alpine@sha256:", proxy_dockerfile)

    def test_herdr_is_pinned_and_is_the_default_environment(self):
        dockerfile = (PATH.parents[1] / "Dockerfile").read_text()
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertIn("ARG HERDR_VERSION=0.8.0", dockerfile)
        self.assertIn("herdr-linux-x86_64", dockerfile)
        self.assertIn("herdr-linux-aarch64", dockerfile)
        self.assertTrue(entrypoint.rstrip().endswith('exec herdr "$@"'))
        self.assertNotIn('exec tact "$@"', entrypoint)

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
        self.assertIn("grep -c '^ssh-'", entrypoint)
        self.assertIn("allowed_fingerprint", entrypoint)
        self.assertIn("backend-api/codex/models?client_version=0.147.0", entrypoint)
        self.assertIn("originator: codex_cli_rs", entrypoint)
        self.assertIn("ChatGPT models request failed with HTTP", entrypoint)
        self.assertIn('mv -f "$config_probe" "$TACT_CONFIG"', entrypoint)
        self.assertIn("successfully authenticated", entrypoint)
        self.assertIn('git -C "$runtime_repo" verify-commit HEAD', entrypoint)
        self.assertIn("tact config show", entrypoint)
        self.assertIn("herdr --version", entrypoint)
        self.assertNotIn("set -x", entrypoint)

    def test_active_v1_is_refused(self):
        result = subprocess.CompletedProcess([], 0, "container-id\n", "")
        with mock.patch.object(D, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "v1 is already running"):
                D.ensure_v1_not_running(Path("/tmp/repo"))

    def test_runtime_doctor_uses_a_temporary_repo_and_checks_teardown(self):
        seen=[]
        def start(repo, runtime_doctor=False):
            self.assertTrue(runtime_doctor)
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

    def test_entrypoint_requires_live_github_and_signing(self):
        entrypoint = (PATH.parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertIn("gh api user", entrypoint)
        self.assertIn("ssh-keygen -Y sign", entrypoint)
        self.assertIn("gpg.ssh.allowedSignersFile /run/devc2-public/allowed_signers", entrypoint)
        self.assertIn("namespaces=\"git\"", PATH.read_text())
        self.assertIn("filtered SSH agent did not become ready", entrypoint)
        self.assertNotIn('$HOME/.zshenv', entrypoint)
        self.assertNotIn('$HOME/.bashrc', entrypoint)
        wrapper = (PATH.parents[1] / "devbox" / "tact-wrapper.sh").read_text()
        self.assertIn("export SSH_AUTH_SOCK=/run/devc2-ssh/agent.sock", wrapper)
        self.assertIn('exec /usr/local/libexec/devc2/tact-upstream "$@"', wrapper)
        self.assertIn("/usr/local/bin:/home/devbox/.local/bin", (PATH.parents[1] / "Dockerfile").read_text())
        self.assertIn("selected 1Password key could not sign", entrypoint)
        self.assertIn("sanitized SSH proxy diagnostics", PATH.read_text())
        self.assertIn("sanitized startup diagnostics", PATH.read_text())

class AuthTests(unittest.TestCase):
    def test_parse_cli_auth_is_a_standalone_command(self):
        with mock.patch.object(D.subprocess, "run") as run:
            self.assertEqual(D.parse_cli(["auth"]), ("auth", None, False))
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
            with mock.patch.object(D, "AUTH", auth), mock.patch.object(D, "require_docker"), mock.patch.object(D, "read_codex", side_effect=[RuntimeError("missing"), ("access", "account")]) as read_codex, mock.patch.object(D, "validate_github_token", side_effect=[RuntimeError("missing"), "user"]) as github_token, mock.patch.object(D, "run", side_effect=auth_run) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.authenticate(), 0)
            self.assertEqual(read_codex.call_count, 2)
            self.assertEqual(github_token.call_count, 2)

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
            self.assertEqual(commands[3][-7:], ["auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"])
            config = auth / "codex" / "config.toml"
            self.assertEqual(config.read_text(), 'cli_auth_credentials_store = "file"\n')
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_read_codex_prefers_managed_auth(self):
        access = D.jwt({"exp": 4102444800})
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
        access = D.jwt({"exp": 4102444800})
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

            with mock.patch.object(D, "AUTH", auth), mock.patch.object(D, "require_docker"), mock.patch.object(D, "validate_github_token", side_effect=[RuntimeError("missing"), "user"]), mock.patch.object(D, "run", side_effect=docker_only), mock.patch.dict(os.environ, {"CODEX_HOME": str(host_codex), "PATH": ""}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.authenticate(), 0)
                self.assertEqual(D.read_codex(), (access, "bootstrapped"))
                self.assertEqual(D.github_token(), "docker-gh-token")

            self.assertTrue(commands)
            self.assertTrue(all(command[0] in {"docker", "gh"} for command in commands))


class CredentialTests(unittest.TestCase):
    def test_github_token_not_logged(self):
        completed=subprocess.CompletedProcess([],0,"secret-token\n","")
        with mock.patch.object(D,"run",return_value=completed): self.assertEqual(D.github_token(),"secret-token")
    def test_proxy_config_uses_file_sources_and_exact_hosts(self):
        doc=D.proxy_config(); raw=str(doc)
        self.assertIn("/run/devc2-private/github-token",raw)
        self.assertIn("api.github.com",raw); self.assertIn("uploads.github.com",raw); self.assertIn("chatgpt.com",raw)
        self.assertNotIn("secret-token",raw)
    def test_key_filter_selects_saved_key(self):
        blob="AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        key=f"ssh-ed25519 {blob} selected"
        with tempfile.TemporaryDirectory() as td, mock.patch.object(D,"CONFIG",Path(td)), mock.patch.object(D,"public_keys",return_value=[key]):
            self.assertEqual(D.select_key(False),key)
            self.assertEqual(stat.S_IMODE((Path(td)/"ssh-key.pub").stat().st_mode),0o600)

class RelayPKITests(unittest.TestCase):
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

class HostAgentRelayTests(unittest.TestCase):
    def test_compose_env_projects_relay_metadata_without_ambient_agent(self):
        with mock.patch.object(D, "docker_env", side_effect=lambda values: values):
            result=D.compose_env(Path("/tmp/repo"),Path("/tmp/private"),Path("/tmp/public"),49152,Path("/tmp/relay-secret"))
        self.assertEqual(result["DEVC2_SSH_RELAY_PORT"], "49152")
        self.assertEqual(result["DEVC2_SSH_RELAY_TLS_DIR"], "/tmp/relay-secret")
        self.assertNotIn("SSH_AUTH_SOCK", result)

    def test_host_relay_uses_ambient_agent_and_mutual_tls(self):
        process=mock.Mock(); process.poll.return_value=None
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); port_file=root/"port"; port_file.write_text("49152")
            with mock.patch.dict(os.environ,{"SSH_AUTH_SOCK":"/tmp/onepassword.sock"}), mock.patch.object(D.os,"stat",return_value=mock.Mock(st_mode=stat.S_IFSOCK)), mock.patch.object(D.subprocess,"Popen",return_value=process) as popen:
                self.assertEqual(D.start_host_agent_relay(port_file,root/"tls",root/"key.pub"),(process,49152))
        command=popen.call_args.args[0]
        self.assertIn("/tmp/onepassword.sock",command)
        self.assertIn("--relay-listen",command)
        self.assertIn("--tls-ca",command)
        self.assertIn("--tls-cert",command)
        self.assertIn("--tls-key",command)
        self.assertNotIn("secret", " ".join(command))
        self.assertIn(str(root/"key.pub"),command)

class CoexistenceTests(unittest.TestCase):
    def test_reset_project_is_v2_only(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td); calls=[]
            with mock.patch.object(D,"compose",side_effect=lambda *a,**k: calls.append(a)), mock.patch.object(D,"STATE",repo/"state"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.reset(repo,True),0)
            self.assertTrue(D.identity(repo)[1].startswith("devc2-")); self.assertIn("down", calls[0])
    def test_source_contains_no_devcontainer_mutation(self):
        source=PATH.read_text()
        self.assertNotIn('mkdir(".devcontainer',source)
        self.assertNotIn('rmtree(".devcontainer',source)

if __name__=="__main__": unittest.main()
