#!/usr/bin/env python3
"""Trusted macOS launcher for devbox v2."""
from __future__ import annotations
import argparse, base64, fcntl, hashlib, json, os, platform, secrets, shutil, signal, stat, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

VERSION="0.1.0"
IRON="ironsh/iron-proxy:0.49.0@sha256:c4628019c24f4cc8d77564a26b7c9cedb00accee6f93d06270e85fb8f9c6a7da"
GH_PLACEHOLDER="devc2-github-placeholder-token"
FAKE_ACCOUNT="00000000-0000-0000-0000-000000000000"
GITHUB_KNOWN_HOSTS='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\ngithub.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=\ngithub.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=\n'
SOURCE_ROOT=Path(__file__).resolve().parents[1]
INSTALLED_ROOT=Path(os.environ.get("DEVC2_SHARE_DIR", Path.home()/".local/share/devc2")).expanduser()
ROOT=INSTALLED_ROOT if (INSTALLED_ROOT/"compose.yaml").is_file() else SOURCE_ROOT

def xdg(name, default): return Path(os.environ.get(name, Path.home()/default)).expanduser()
CONFIG=xdg("XDG_CONFIG_HOME", ".config")/"devc2"
STATE=xdg("XDG_STATE_HOME", ".local/state")/"devc2"
AUTH=CONFIG/"auth"
TACT_CONFIG=CONFIG/"tact"/"config.toml"
TACT_CONFIG_DEFAULT="[agent]\nmax_subagents = 8\n"
AUTH_IMAGE="devc2:0.1.0"

def run(argv, *, env=None, capture=True, check=True, timeout=120):
    try:
        return subprocess.run(argv, env=env, text=True, capture_output=capture, check=check, timeout=timeout)
    except FileNotFoundError as e: raise RuntimeError(f"required command not found: {argv[0]}") from e
    except subprocess.CalledProcessError as e:
        detail=(e.stderr or e.stdout or "command failed").strip()
        raise RuntimeError(f"{argv[0]} failed: {detail}") from e

def docker_env(extra=None):
    keep={k:v for k,v in os.environ.items() if k in {"PATH","HOME","DOCKER_HOST","DOCKER_CONTEXT","DOCKER_CONFIG","XDG_RUNTIME_DIR"}}
    if extra: keep.update(extra)
    return keep

def require_docker():
    if platform.system() != "Darwin": raise RuntimeError("devc2 currently supports macOS only")
    run(["docker","compose","version"],timeout=15)
    host=run(["docker","context","inspect","--format","{{.Endpoints.docker.Host}}"],timeout=15).stdout.strip()
    if not (host.startswith("unix://") or host.startswith("/")): raise RuntimeError(f"devc2 requires a local Docker socket, got {host!r}")
    if run(["docker","info","--format","{{.OSType}}"],timeout=15).stdout.strip()!="linux": raise RuntimeError("devc2 requires Docker Desktop Linux containers")

def canonical_repo(raw):
    path = Path(raw).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise RuntimeError(f"repository is not a directory: {path}")
    if not ((path / ".git").exists() or (path / ".jj").exists()):
        raise RuntimeError(f"workspace must be a Git or Jujutsu checkout: {path}")
    home = Path.home().resolve()
    if path == home or home.is_relative_to(path):
        raise RuntimeError("refusing to mount the host home directory or one of its parents")
    sensitive = [
        Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser().resolve(),
        (home / ".config" / "gh").resolve(),
        CONFIG.resolve(),
    ]
    for secret_root in sensitive:
        if secret_root == path or secret_root.is_relative_to(path):
            raise RuntimeError(f"credential directory would be exposed by workspace mount: {secret_root}")
    return path

def identity(repo):
    digest=hashlib.sha256(os.fsencode(repo)).hexdigest()[:16]
    return digest, f"devc2-{digest}"

def jwt(claims):
    enc=lambda x:base64.urlsafe_b64encode(json.dumps(x,separators=(",",":")).encode()).decode().rstrip("=")
    return f"{enc({'alg':'none','typ':'JWT'})}.{enc(claims)}.signature"
FAKE_ACCESS=jwt({'aud':['https://api.openai.com/v1','https://chatgpt.com/backend-api'],'exp':4102444800,'sub':'devc2'})
FAKE_ID=jwt({'exp':4102444800,'https://api.openai.com/auth':{'chatgpt_account_id':FAKE_ACCOUNT,'chatgpt_plan_type':'plus'},'sub':'devc2'})

def ensure_tact_config():
    TACT_CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        private_write(TACT_CONFIG, TACT_CONFIG_DEFAULT)
    except FileExistsError:
        pass
    metadata = TACT_CONFIG.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"shared Tact config must be a regular file: {TACT_CONFIG}")
    return TACT_CONFIG

def private_write(path, data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try: os.write(fd,data if isinstance(data,bytes) else data.encode())
    finally: os.close(fd)

def read_codex():
    managed = AUTH / "codex" / "auth.json"
    path = managed if managed.exists() else Path(os.environ.get("CODEX_HOME",Path.home()/".codex"))/"auth.json"
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise RuntimeError("Codex auth must be a regular file smaller than 1 MiB")
        with os.fdopen(descriptor, "r", encoding="utf-8") as auth_file:
            doc = json.load(auth_file)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"valid ChatGPT Codex login required at {path}; run `devc2 auth`") from e
    tokens=doc.get("tokens",doc)
    access=tokens.get("access_token"); account=tokens.get("account_id")
    if not access or not account: raise RuntimeError(f"ChatGPT Codex login at {path} lacks access_token/account_id")
    try:
        body=access.split('.')[1]; body+='='*(-len(body)%4); exp=json.loads(base64.urlsafe_b64decode(body))["exp"]
        if exp < time.time()+3600: raise RuntimeError("ChatGPT access token expires within one hour; run codex login on the host")
    except RuntimeError: raise
    except Exception as e: raise RuntimeError("ChatGPT access token is malformed") from e
    return access,account

def github_token():
    managed = AUTH / "gh"
    if (managed / "hosts.yml").exists():
        value = run([
            "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp", "--env", "GH_CONFIG_DIR=/run/devc2-gh",
            "--env", "GH_TOKEN=", "--env", "GITHUB_TOKEN=",
            "--volume", f"{managed}:/run/devc2-gh:ro",
            "--entrypoint", "gh", AUTH_IMAGE, "auth", "token",
        ], timeout=30).stdout.strip()
    else:
        try:
            value = run(["gh", "auth", "token"], timeout=15).stdout.strip()
        except RuntimeError as error:
            raise RuntimeError("not configured; run `devc2 auth`") from error
    if not value or any(c.isspace() for c in value):
        raise RuntimeError("GitHub login is invalid; run `devc2 auth`")
    return value


def validate_github_token():
    token = github_token()
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "devc2-doctor",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            document = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as error:
        raise RuntimeError("credential was rejected by GitHub; run `devc2 auth`") from error
    login = document.get("login")
    if not isinstance(login, str) or not login:
        raise RuntimeError("GitHub returned an invalid authenticated identity")
    return login


def public_keys():
    agent = os.environ.get("SSH_AUTH_SOCK", "")
    if not agent or not Path(agent).exists():
        raise RuntimeError("SSH_AUTH_SOCK is not an available Unix socket; enable the 1Password SSH agent")
    out=run(["ssh-add","-L"],timeout=15).stdout
    return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("The agent has")]

def key_blob(line):
    fields=line.split()
    if len(fields)<2: raise RuntimeError("invalid public key returned by SSH agent")
    return base64.b64decode(fields[1],validate=True)

def fingerprint(line):
    digest=base64.b64encode(hashlib.sha256(key_blob(line)).digest()).decode().rstrip('=')
    return f"SHA256:{digest}"

def select_key(interactive=True):
    keys=public_keys()
    if not keys: raise RuntimeError("1Password SSH agent has no available keys")
    CONFIG.mkdir(parents=True,exist_ok=True,mode=0o700)
    selected_path=CONFIG/"ssh-key.pub"
    if selected_path.exists():
        saved=selected_path.read_text().strip()
        if any(key_blob(k)==key_blob(saved) for k in keys): return saved
        raise RuntimeError(f"configured key {fingerprint(saved)} is not available from 1Password; remove {selected_path} to select again")
    if len(keys)==1: selected=keys[0]
    elif not interactive: raise RuntimeError(f"{len(keys)} SSH keys available; start devc2 once to select one")
    else:
        print("Select the 1Password SSH key for devc2:",file=sys.stderr)
        for i,k in enumerate(keys,1): print(f"  {i}. {fingerprint(k)} {' '.join(k.split()[2:])}",file=sys.stderr)
        try: index=int(input("Key number: "))
        except (ValueError,EOFError): raise RuntimeError("invalid key selection")
        if not 1<=index<=len(keys): raise RuntimeError("invalid key selection")
        selected=keys[index-1]
    private_write(selected_path,(selected+"\n").encode()); os.chmod(selected_path,0o600)
    return selected

def proxy_config():
    def secret(file,placeholder,header,host,paths,methods):
        return {'source':{'type':'file','path':f'/run/devc2-private/{file}'},'replace':{'proxy_value':placeholder,'match_headers':[header],'require':True},'rules':[{'host':host,'methods':methods,'paths':paths}]}
    methods=['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']
    secrets=[
      secret('access-token',FAKE_ACCESS,'Authorization','chatgpt.com',['/backend-api/codex','/backend-api/codex/*'],['GET','POST']),
      secret('account-id',FAKE_ACCOUNT,'chatgpt-account-id','chatgpt.com',['/backend-api/codex','/backend-api/codex/*'],['GET','POST']),
      secret('github-token',GH_PLACEHOLDER,'Authorization','api.github.com',['/','/*'],methods),
      secret('github-token',GH_PLACEHOLDER,'Authorization','uploads.github.com',['/','/*'],methods),
    ]
    return {'dns':{'enabled':False},'proxy':{'tunnel_listen':'127.0.0.1:8080','http_listen':'127.0.0.1:0','https_listen':'127.0.0.1:0'},'metrics':{'listen':'127.0.0.1:0'},'tls':{'mode':'mitm','ca_cert':'/run/devc2-private/ca.crt','ca_key':'/run/devc2-private/ca.key'},'transforms':[{'name':'secrets','config':{'secrets':secrets}}],'log':{'level':'warn'}}

def fake_auth():
    return {'OPENAI_API_KEY':None,'auth_mode':'chatgpt','tokens':{'id_token':FAKE_ID,'access_token':FAKE_ACCESS,'refresh_token':'not-a-real-refresh-token','account_id':FAKE_ACCOUNT},'last_refresh':'2025-01-01T00:00:00.000Z'}

def generate_ca(private):
    run(['docker','run','--rm','--network','none','--volume',f'{private}:/out',IRON,'generate-ca','-outdir','/out','-alg','ed25519','-name','devbox v2'],env=docker_env(),timeout=120)
    for name in ('ca.crt','ca.key'):
        p=private/name; s=p.lstat()
        if stat.S_ISLNK(s.st_mode) or not stat.S_ISREG(s.st_mode): raise RuntimeError(f"proxy generated invalid {name}")
        p.chmod(0o600)

def generate_relay_pki(root):
    openssl='/usr/bin/openssl'
    if not Path(openssl).is_file() or not os.access(openssl,os.X_OK):
        raise RuntimeError("/usr/bin/openssl is required for the SSH relay")
    server=root/'server'; client=root/'client'; server.mkdir(parents=True,mode=0o700); client.mkdir(mode=0o700)
    ca_config=server/'ca.cnf'; server_ext=server/'server.ext'; client_ext=server/'client.ext'
    private_write(ca_config,"[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=v3\n[dn]\nCN=devc2 ephemeral relay CA\n[v3]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    private_write(server_ext,"[v3]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:host.docker.internal\n")
    private_write(client_ext,"[v3]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n")
    run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','30','-config',str(ca_config),'-keyout',str(server/'ca.key'),'-out',str(server/'ca.crt')],timeout=60)
    for name,common_name,extensions,serial in (
        ('server','host.docker.internal',server_ext,str(secrets.randbits(128) or 1)),
        ('client','devc2 ssh proxy',client_ext,str(secrets.randbits(128) or 1)),
    ):
        key=server/f'{name}.key'; csr=server/f'{name}.csr'; cert=server/f'{name}.crt'
        run([openssl,'req','-new','-newkey','rsa:2048','-nodes','-sha256','-subj',f'/CN={common_name}','-keyout',str(key),'-out',str(csr)],timeout=60)
        run([openssl,'x509','-req','-sha256','-days','30','-in',str(csr),'-CA',str(server/'ca.crt'),'-CAkey',str(server/'ca.key'),'-set_serial',serial,'-extfile',str(extensions),'-extensions','v3','-out',str(cert)],timeout=60)
    for name in ('ca.crt','server.crt','server.key','client.crt','client.key'):
        path=server/name; metadata=path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size>1024*1024:
            raise RuntimeError(f"OpenSSL generated invalid relay file: {name}")
    for name in ('ca.crt','client.crt','client.key'):
        shutil.copyfile(server/name,client/name)
    for path in server.iterdir():
        if path.name not in {'ca.crt','server.crt','server.key'}: path.unlink()
    for path in server.iterdir(): path.chmod(0o600 if path.suffix=='.key' else 0o644)
    for path in client.iterdir(): path.chmod(0o444)
    return server,client

def compose_env(repo,private,public,relay_port=None,relay_tls_dir=None):
    digest,project=identity(repo)
    values={'DEVC2_PROJECT_NAME':project,'DEVC2_WORKSPACE':str(repo),'DEVC2_WORKSPACE_HASH':digest,'DEVC2_PRIVATE_DIR':str(private),'DEVC2_PUBLIC_DIR':str(public),'DEVC2_INSTALL_DIR':str(ROOT),'DEVC2_IMAGE':'devc2:0.1.0','DEVC2_UID':str(os.getuid()),'DEVC2_GID':str(os.getgid())}
    values['DEVC2_SSH_RELAY_PORT']=str(relay_port or 1)
    values['DEVC2_SSH_RELAY_TLS_DIR']=str(relay_tls_dir or '/dev/null')
    return docker_env(values)

def start_host_agent_relay(port_file, tls_dir, allowed_key_file):
    upstream=os.environ.get("SSH_AUTH_SOCK", "")
    try: available=bool(upstream) and stat.S_ISSOCK(os.stat(upstream).st_mode)
    except OSError: available=False
    if not available:
        raise RuntimeError("SSH_AUTH_SOCK is not an available Unix socket; enable the 1Password SSH agent")
    command=[sys.executable,str(ROOT/'credential_proxy'/'ssh_agent_proxy.py'),'--relay-listen','0.0.0.0:0','--relay-port-file',str(port_file),'--upstream',upstream,'--tls-ca',str(tls_dir/'ca.crt'),'--tls-cert',str(tls_dir/'server.crt'),'--tls-key',str(tls_dir/'server.key'),'--allowed-key',str(allowed_key_file)]
    process=subprocess.Popen(command)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if process.poll() is not None: raise RuntimeError("host SSH-agent relay exited before it was ready")
        try:
            port=int(port_file.read_text())
            if 1 <= port <= 65535: return process,port
        except (FileNotFoundError,ValueError): pass
        time.sleep(.05)
    stop_process(process)
    raise RuntimeError("host SSH-agent relay did not become ready")

def stop_process(process):
    if process is None or process.poll() is not None: return
    process.terminate()
    try: process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=5)

def prepare(repo,temp):
    private=temp/'private'; public=temp/'public'; private.mkdir(0o700); public.mkdir(0o700)
    access,account=read_codex(); gh=github_token(); key=select_key(True)
    generate_ca(private)
    private_write(private/'access-token',access); private_write(private/'account-id',account); private_write(private/'github-token',gh)
    private_write(private/'proxy.yaml',json.dumps(proxy_config(),separators=(',',':')))
    shutil.copyfile(private/'ca.crt',public/'ca.crt'); (public/'ca.crt').chmod(0o644)
    (public/'auth.json').write_text(json.dumps(fake_auth(),separators=(',',':'))); (public/'auth.json').chmod(0o644)
    (public/'codex').mkdir(); shutil.copyfile(public/'auth.json',public/'codex'/'auth.json'); (public/'codex'/'auth.json').chmod(0o644)
    shutil.copyfile(TACT_CONFIG, public / 'tact-config.toml')
    (public / 'tact-config.toml').chmod(0o644)
    (public/'ssh-allowed.pub').write_text(key+'\n'); (public/'ssh-allowed.pub').chmod(0o644)
    key_fields=key.split()
    (public/'allowed_signers').write_text(f'* namespaces="git" {key_fields[0]} {key_fields[1]}\n')
    (public/'allowed_signers').chmod(0o644)
    (public/'known_hosts').write_text(GITHUB_KNOWN_HOSTS); (public/'known_hosts').chmod(0o644)
    gh_config = public / 'gh'
    gh_config.mkdir()
    (gh_config / 'hosts.yml').write_text(
        'github.com:\n'
        '  git_protocol: ssh\n'
        f'  oauth_token: {GH_PLACEHOLDER}\n'
        '  user: devc2\n'
    )
    (gh_config / 'hosts.yml').chmod(0o644)
    return private,public

def compose(repo,env,*args,capture=False,check=True):
    cmd=['docker','compose','--project-name',identity(repo)[1],'-f',str(ROOT/'compose.yaml'),*args]
    return run(cmd,env=env,capture=capture,check=check,timeout=1800)

def ensure_v1_not_running(repo):
    result = run(["docker", "ps", "--filter", f"label=devcontainer.local_folder={repo}", "--format", "{{.ID}}"], timeout=15)
    if result.stdout.strip():
        raise RuntimeError("v1 is already running for this checkout; stop it before starting devc2")


def start(repo, runtime_doctor=False):
    require_docker()
    ensure_v1_not_running(repo)
    ensure_tact_config()
    digest, _project = identity(repo)
    lock_directory = STATE / digest
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (lock_directory / "launch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("devc2 is already running for this checkout") from error
        with tempfile.TemporaryDirectory(prefix='devc2-') as raw:
            temp=Path(raw); temp.chmod(0o700); private,public=prepare(repo,temp)
            relay_server_dir,relay_client_dir=generate_relay_pki(temp/'ssh-relay-pki')
            host_agent_relay,relay_port=start_host_agent_relay(temp/'ssh-relay-port',relay_server_dir,public/'ssh-allowed.pub')
            try:
                env=compose_env(repo,private,public,relay_port,relay_client_dir)
            except Exception:
                stop_process(host_agent_relay)
                raise
            stopping = False
            previous = {}
            def terminate(signum, _frame):
                nonlocal stopping
                stopping = True
                raise KeyboardInterrupt(f"signal {signum}")
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, terminate)
            try:
                compose(repo,env,'build','devbox','ssh-proxy')
                try:
                    compose(repo,env,'up','-d','credential-proxy','ssh-proxy')
                except RuntimeError:
                    print("devc2: sanitized startup diagnostics:",file=sys.stderr)
                    compose(repo,env,'logs','--no-color','--tail','100','ssh-proxy',capture=False,check=False)
                    raise
                command=['docker','compose','--project-name',identity(repo)[1],'-f',str(ROOT/'compose.yaml'),'run','--rm','--no-deps']
                if runtime_doctor: command.extend(['--env','DEVC2_RUNTIME_DOCTOR=1'])
                command.append('devbox')
                result=subprocess.run(command,env=env).returncode
                if result:
                    print("devc2: sanitized SSH proxy diagnostics:",file=sys.stderr)
                    compose(repo,env,'logs','--no-color','--tail','100','ssh-proxy',capture=False,check=False)
                return result
            except KeyboardInterrupt:
                return 130 if stopping else 1
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                stop_process(host_agent_relay)
                try:
                    down_args=['down','--remove-orphans']
                    if runtime_doctor: down_args.append('--volumes')
                    compose(repo,env,*down_args,check=False)
                finally:
                    if runtime_doctor: shutil.rmtree(lock_directory,ignore_errors=True)

def reset(repo,yes):
    if not yes:
        try: answer=input(f"Remove v2 state for {repo}? [y/N] ")
        except EOFError: answer=''
        if answer.lower() not in {'y','yes'}: print('cancelled'); return 0
    digest,project=identity(repo)
    dummy=STATE/'reset'; dummy.mkdir(parents=True,exist_ok=True)
    env=compose_env(repo,dummy,dummy)
    compose(repo,env,'down','--volumes','--remove-orphans',check=False)
    shutil.rmtree(STATE/digest,ignore_errors=True)
    print(f"reset {project}"); return 0

def authenticate():
    require_docker()
    AUTH.mkdir(parents=True, exist_ok=True, mode=0o700)
    codex_home = AUTH / "codex"
    gh_home = AUTH / "gh"
    codex_home.mkdir(exist_ok=True, mode=0o700)
    gh_home.mkdir(exist_ok=True, mode=0o700)
    codex_config = codex_home / "config.toml"
    if not codex_config.exists():
        private_write(codex_config, 'cli_auth_credentials_store = "file"\n')

    codex_ready = False
    github_ready = False
    try:
        read_codex()
        codex_ready = True
    except RuntimeError:
        pass
    try:
        validate_github_token()
        github_ready = True
    except RuntimeError:
        pass

    if codex_ready and github_ready:
        print("ChatGPT and GitHub authentication are already configured.")
        return 0

    print("Preparing the pinned authentication image…")
    run(["docker", "build", "--tag", AUTH_IMAGE, str(ROOT)], capture=False, timeout=1800)

    if not codex_ready:
        print("Signing in to ChatGPT for Codex…")
        run([
            "docker", "run", "--rm", "-it", "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp/devc2-login", "--env", "CODEX_HOME=/home/devbox/.codex",
            "--volume", f"{codex_home}:/home/devbox/.codex",
            "--entrypoint", "codex", AUTH_IMAGE,
            "login", "--device-auth",
        ], capture=False, timeout=900)
        read_codex()
    else:
        print("ChatGPT authentication is already configured; skipping.")

    if not github_ready:
        print("Signing in to GitHub CLI…")
        # The Debian-packaged gh used in the pinned image predates
        # --insecure-storage. The minimal container has no credential-store
        # helper, so gh falls back to the mounted hosts.yml file.
        run([
            "docker", "run", "--rm", "-it", "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp", "--env", "GH_CONFIG_DIR=/run/devc2-gh",
            "--env", "GH_TOKEN=", "--env", "GITHUB_TOKEN=",
            "--volume", f"{gh_home}:/run/devc2-gh",
            "--entrypoint", "gh", AUTH_IMAGE,
            "auth", "login", "--hostname", "github.com",
            "--git-protocol", "https", "--web",
        ], capture=False, timeout=900)
        validate_github_token()
    else:
        print("GitHub authentication is already configured; skipping.")

    print("Authentication saved outside all workspaces.")
    return 0

def doctor(runtime=False):
    checks=[]
    def check(label,fn):
        try: detail=fn(); checks.append((True,label,detail or 'ok'))
        except Exception as e: checks.append((False,label,str(e)))
    check('Docker Desktop',lambda: require_docker())
    check('OpenSSL',lambda: run(['/usr/bin/openssl','version'],timeout=10).stdout.strip())
    check('ChatGPT Codex login',lambda: (read_codex() and 'access token has more than one hour remaining'))
    check('GitHub CLI OAuth',lambda: f"authenticated as {validate_github_token()}")
    def check_agent():
        keys = public_keys()
        if not keys:
            raise RuntimeError("no keys available; enable the 1Password SSH agent")
        return f"{len(keys)} key(s) available"
    check('1Password SSH agent', check_agent)
    for ok,label,detail in checks: print(f"{'✓' if ok else '✗'} {label}: {detail}")
    if not all(x[0] for x in checks): return 1
    if not runtime: return 0
    print("Running disposable end-to-end runtime checks…")
    with tempfile.TemporaryDirectory(prefix='devc2-doctor-repo-') as raw:
        repo=Path(raw); (repo/'.git').mkdir(); project=identity(repo)[1]
        result=start(repo,runtime_doctor=True)
        leftovers=[]
        resource_commands={
            'containers':['docker','container','ls','-aq'],
            'volumes':['docker','volume','ls','-q'],
            'networks':['docker','network','ls','-q'],
        }
        for kind,command in resource_commands.items():
            found=run([*command,'--filter',f'label=com.docker.compose.project={project}'],timeout=15).stdout.split()
            if found: leftovers.append(f"{kind}={len(found)}")
        if leftovers: raise RuntimeError(f"runtime doctor teardown left resources: {', '.join(leftovers)}")
        return result

def usage_parser():
    parser = argparse.ArgumentParser(
        prog="devc2",
        description="Open Herdr in a hardened, credential-isolated devbox.",
        epilog="commands: devc2 <repo> | devc2 auth | devc2 doctor [--runtime] | devc2 reset <repo> [--yes]",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def parse_cli(argv):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        usage_parser().print_help()
        raise SystemExit(0 if argv else 2)
    if argv[0] == "--version":
        print(f"devc2 {VERSION}")
        raise SystemExit(0)
    if argv[0] == "auth":
        if len(argv) != 1: raise RuntimeError("auth accepts no arguments")
        return "auth", None, False
    if argv[0] == "doctor":
        parser=argparse.ArgumentParser(prog="devc2 doctor")
        parser.add_argument("--runtime",action="store_true",help="run disposable end-to-end Docker checks")
        args=parser.parse_args(argv[1:])
        return "doctor-runtime" if args.runtime else "doctor", None, False
    if argv[0] == "reset":
        parser = argparse.ArgumentParser(prog="devc2 reset")
        parser.add_argument("repo")
        parser.add_argument("--yes", action="store_true")
        args = parser.parse_args(argv[1:])
        return "reset", args.repo, args.yes
    if argv[0].startswith("-") or len(argv) != 1:
        raise RuntimeError("usage: devc2 <repo> | devc2 auth | devc2 doctor [--runtime] | devc2 reset <repo> [--yes]")
    return "start", argv[0], False


def main(argv=None):
    try:
        command, raw_repo, yes = parse_cli(argv)
        if command in {"doctor","doctor-runtime"}:
            return doctor(runtime=command=="doctor-runtime")
        if command == "auth":
            return authenticate()
        repo = canonical_repo(raw_repo)
        if command == "reset":
            return reset(repo, yes)
        return start(repo)
    except RuntimeError as error:
        print(f"devc2: error: {error}", file=sys.stderr)
        return 1

if __name__=='__main__': raise SystemExit(main())
