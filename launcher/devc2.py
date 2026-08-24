#!/usr/bin/env python3
"""Trusted macOS launcher for devc2."""
from __future__ import annotations
import argparse, base64, contextlib, errno, fcntl, gzip, hashlib, json, os, platform, re, secrets, shlex, shutil, signal, stat, subprocess, sys, tarfile, tempfile, time, unicodedata, urllib.error, urllib.parse, urllib.request
from pathlib import Path, PurePosixPath
from typing import NamedTuple

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import span_runtime
from asset_contract import ASSET_ROOTS, EXECUTABLE_ASSETS, REQUIRED_ASSETS, validate_required_assets

VERSION="0.2.0"
SOURCE_ROOT=Path(__file__).resolve().parents[1]
INSTALLED_ROOT=Path(os.environ.get("DEVC2_RUNTIME_ROOT",os.environ.get("DEVC2_SHARE_DIR",Path.home()/".local/share/devc2"))).expanduser().resolve()
ROOT=INSTALLED_ROOT if (INSTALLED_ROOT/"compose.yaml").is_file() else SOURCE_ROOT
BUILTIN_AGENT_SKILLS=ROOT/"box"/"agent-skills"

def asset_digest(source):
    digest=hashlib.sha256(); runtime_roots=set(ASSET_ROOTS)
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        relative=path.relative_to(source)
        if relative.parts[0] not in runtime_roots or "tests" in relative.parts or "__pycache__" in relative.parts: continue
        name=str(relative).encode(); content=path.read_bytes()
        digest.update(len(name).to_bytes(4,"big")); digest.update(name)
        digest.update(len(content).to_bytes(8,"big")); digest.update(content)
    return digest.hexdigest()

ASSET_DIGEST=asset_digest(ROOT)
IMAGE_LABEL="dev.devc2.asset-digest"
IMAGE_TAG_SUFFIX=f"{VERSION}-{ASSET_DIGEST[:12]}"

def xdg(name, default): return Path(os.environ.get(name, Path.home()/default)).expanduser()
CONFIG=xdg("XDG_CONFIG_HOME", ".config")/"devc2"
AGENT_SKILLS=xdg("XDG_CONFIG_HOME", ".config")/"agent-skills"
STATE=xdg("XDG_STATE_HOME", ".local/state")/"devc2"
AUTH=CONFIG/"auth"
TACT_CONFIG=CONFIG/"tact"/"config.toml"
SPAN_CATALOG=CONFIG/"spans.json"
TACT_CONFIG_DEFAULT="[agent]\nmax_subagents = 8\n"
RUNTIME_IMAGE=f"devc2:{IMAGE_TAG_SUFFIX}"
AUTH_IMAGE=f"devc2-auth:{IMAGE_TAG_SUFFIX}"
SPAN_GATEWAY_IMAGE=f"devc2-span-proxy:{IMAGE_TAG_SUFFIX}"
INSTALL_STATE=STATE/"installation.json"
INSTALL_LOCK=STATE/"install.lock"
UPDATE_LOCK=STATE/"update.lock"
RELEASE_API="https://api.github.com/repos/yum0e/devbox/releases/latest"
RELEASE_ASSET="devc2.tar.gz"
MAX_RELEASE_BYTES=32*1024*1024
MAX_RELEASE_TAR_BYTES=40*1024*1024
MAX_SKILL_FILES=512
MAX_SKILL_BYTES=16*1024*1024
MAX_SKILL_FILE_BYTES=4*1024*1024

def run(argv, *, env=None, capture=True, check=True, timeout=120):
    try:
        return subprocess.run(argv, env=env, text=True, capture_output=capture, check=check, timeout=timeout)
    except FileNotFoundError as e: raise RuntimeError(f"required command not found: {argv[0]}") from e
    except subprocess.CalledProcessError as e:
        detail=(e.stderr or e.stdout or "command failed").strip()
        raise RuntimeError(f"{argv[0]} failed: {detail}") from e

@contextlib.contextmanager
def lifecycle_lock(exclusive):
    inherited=os.environ.get("DEVC2_CONTROL_LOCK_FD")
    if inherited:
        try:
            descriptor=int(inherited); opened=os.fstat(descriptor); expected=INSTALL_LOCK.stat()
            if (opened.st_dev,opened.st_ino)!=(expected.st_dev,expected.st_ino): raise OSError
        except (ValueError,OSError,FileNotFoundError) as error:
            raise RuntimeError("invalid inherited installation lock") from error
        mode=os.environ.get("DEVC2_CONTROL_LOCK_MODE")
        if mode not in {"shared","exclusive"}: raise RuntimeError("invalid inherited installation lock")
        if exclusive and mode!="exclusive": raise RuntimeError("management command requires the exclusive installation lock")
        operation=fcntl.LOCK_EX if mode=="exclusive" else fcntl.LOCK_SH
        try: fcntl.flock(descriptor,operation|fcntl.LOCK_NB)
        except BlockingIOError as error: raise RuntimeError("inherited installation lock is not held") from error
        yield
        return
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with INSTALL_LOCK.open("a+") as lock:
        operation=fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try: fcntl.flock(lock,operation|fcntl.LOCK_NB)
        except BlockingIOError as error:
            if exclusive: raise RuntimeError("cannot install or roll back while a devc2 session is active") from error
            raise RuntimeError("cannot start or update while devc2 installation management is active") from error
        try: yield
        finally: fcntl.flock(lock,fcntl.LOCK_UN)

@contextlib.contextmanager
def exclusive_lock(path,busy_message):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open("a+") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error: raise RuntimeError(busy_message) from error
        try: yield
        finally: fcntl.flock(lock,fcntl.LOCK_UN)

@contextlib.contextmanager
def forward_update_lock():
    with exclusive_lock(UPDATE_LOCK,"another devc2 update is already active"):
        yield

@contextlib.contextmanager
def checkout_lock(repo):
    locks=STATE/"locks"; locks.mkdir(parents=True,exist_ok=True,mode=0o700)
    digest=identity(repo)[0]
    legacy_directory=STATE/digest; legacy_directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (locks/f"{digest}.lock").open("a+") as lock, (legacy_directory/"launch.lock").open("a+") as legacy:
        acquired=[]
        try:
            for candidate in (lock,legacy):
                try: fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB); acquired.append(candidate)
                except BlockingIOError as error: raise RuntimeError("devc2 is already running for this checkout") from error
            yield
        finally:
            for candidate in reversed(acquired): fcntl.flock(candidate,fcntl.LOCK_UN)

@contextlib.contextmanager
def repair_lock(repo):
    path=STATE/"repairs"/f"{identity(repo)[0]}.lock"
    with exclusive_lock(path,"another repair is already active for this checkout"):
        yield

@contextlib.contextmanager
def skills_refresh_lock(repo):
    path=STATE/"skill-refresh"/f"{identity(repo)[0]}.lock"
    with exclusive_lock(path,"another skill refresh is already active for this checkout"):
        yield

def active_legacy_sessions():
    active=[]
    if not STATE.exists(): return active
    for path in STATE.glob("*/launch.lock"):
        try:
            with path.open("r") as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: active.append(path.parent.name)
                else: fcntl.flock(lock,fcntl.LOCK_UN)
        except FileNotFoundError:
            continue
        except OSError:
            active.append(path.parent.name)
    return active

def atomic_write(path,data,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor,temp_name=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    temp=Path(temp_name)
    try:
        os.fchmod(descriptor,mode)
        payload=data if isinstance(data,bytes) else data.encode()
        with os.fdopen(descriptor,"wb") as output:
            output.write(payload); output.flush(); os.fsync(output.fileno())
        os.replace(temp,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except Exception:
        try: os.close(descriptor)
        except OSError: pass
        temp.unlink(missing_ok=True)
        raise

def _make_tree_removable(path):
    if not path.exists():
        return

    for directory, _dirs, _files in os.walk(
        path, topdown=False, followlinks=False
    ):
        try:
            Path(directory).chmod(0o700)
        except FileNotFoundError:
            pass


def _remove_launch_directory(path):
    deadline = time.monotonic() + 5

    while True:
        try:
            _make_tree_removable(path)
            shutil.rmtree(path)
            return

        except FileNotFoundError:
            return

        except OSError as error:
            if error.errno not in {errno.EBUSY, errno.ENOTEMPTY}:
                raise RuntimeError(
                    f"could not remove the temporary launch directory: {error}"
                ) from error

            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Docker did not release the temporary launch directory after teardown"
                ) from error

            time.sleep(0.05)


def _report_launch_cleanup_failure(launch_error, cleanup_error):
    note = f"temporary launch directory cleanup also failed: {cleanup_error}"
    add_note = getattr(launch_error, "add_note", None)
    if callable(add_note):
        add_note(note)
    else:
        print(f"devc2: warning: {note}", file=sys.stderr)


@contextlib.contextmanager
def launch_directory():
    path = Path(tempfile.mkdtemp(prefix="devc2-"))
    path.chmod(0o700)

    try:
        yield path
    except BaseException as launch_error:
        try:
            _remove_launch_directory(path)
        except Exception as cleanup_error:
            _report_launch_cleanup_failure(launch_error, cleanup_error)
        raise
    else:
        _remove_launch_directory(path)

def source_version(source):
    import re
    launcher=source/"launcher"/"devc2.py"
    try: text=launcher.read_text(encoding="utf-8")
    except OSError as error: raise RuntimeError("release lacks launcher/devc2.py") from error
    match=re.search(r'^VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',text,re.MULTILINE)
    if not match: raise RuntimeError("release has an invalid VERSION")
    return match.group(1)

def validate_asset_tree(source,strict=False):
    if not source.is_dir() or source.is_symlink(): raise RuntimeError("release asset root must be a directory")
    total=0; count=0; allowed=set(ASSET_ROOTS)
    for path in source.rglob("*"):
        relative=path.relative_to(source)
        unexpected=not relative.parts or relative.parts[0] not in allowed or "__pycache__" in relative.parts or "tests" in relative.parts
        if unexpected:
            if strict: raise RuntimeError(f"release contains an unexpected path: {relative}")
            continue
        count+=1
        if count>256: raise RuntimeError("release contains too many files")
        metadata=path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise RuntimeError(f"release contains an unsupported file: {path.relative_to(source)}")
        if stat.S_ISREG(metadata.st_mode):
            total+=metadata.st_size
            if metadata.st_size>MAX_RELEASE_BYTES or total>MAX_RELEASE_BYTES:
                raise RuntimeError("release assets exceed the size limit")
    validate_required_assets(source)
    return source_version(source)

def installation_record(data_root=None):
    root=Path(os.environ.get("DEVC2_DATA_DIR",Path.home()/".local/share/devc2")) if data_root is None else Path(data_root)
    return root.expanduser().absolute()/"installation.json"

def _validate_installation_record(document,path):
    if not isinstance(document,dict) or document.get("schema")!=2:
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    required_strings=("current","manager","version","bin_dir","share_dir","install_kind")
    if any(not isinstance(document.get(key),str) or not document[key] for key in required_strings):
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    for key in ("current","manager","bin_dir","share_dir"):
        if not Path(document[key]).is_absolute(): raise RuntimeError(f"installed runtime record is invalid: {path}")
    for key in ("previous","previous_version","source_dir","previous_install_kind","previous_source_dir"):
        if document.get(key) is not None and not isinstance(document[key],str):
            raise RuntimeError(f"installed runtime record is invalid: {path}")
    if document.get("previous") is not None and not Path(document["previous"]).is_absolute():
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    if Path(document["share_dir"])!=path.parent:
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    roles=[document["current"],document["manager"]]
    if document.get("previous") is not None: roles.append(document["previous"])
    for value in roles:
        runtime=Path(value)
        try: resolved=runtime.resolve(strict=True)
        except OSError as error: raise RuntimeError(f"installed runtime record is invalid: {path}") from error
        if resolved!=runtime or not runtime.is_dir() or runtime.is_symlink():
            raise RuntimeError(f"installed runtime record is invalid: {path}")
        launcher=runtime/"launcher"/"devc2.py"
        if not launcher.is_file() or launcher.is_symlink():
            raise RuntimeError(f"installed runtime record is invalid: {path}")
    if document["manager"] not in {document["current"],document.get("previous")}:
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    return document

def read_installation_state(data_root=None):
    """Read the authoritative record, or schema-1 metadata during migration only."""
    path=installation_record(data_root)
    try:
        metadata=path.lstat()
    except FileNotFoundError:
        try:
            document=json.loads(INSTALL_STATE.read_text(encoding="utf-8"))
            return document if isinstance(document,dict) and document.get("schema")==1 else {}
        except (FileNotFoundError,OSError,ValueError): return {}
    except OSError as error:
        raise RuntimeError(f"installed runtime record cannot be read: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    if metadata.st_size>64*1024 or metadata.st_uid!=os.getuid() or metadata.st_mode & 0o022:
        raise RuntimeError(f"installed runtime record is invalid: {path}")
    try: document=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError) as error: raise RuntimeError(f"installed runtime record is invalid: {path}") from error
    return _validate_installation_record(document,path)

def write_installation_record(share_dir,document):
    atomic_write(installation_record(share_dir),json.dumps(document,sort_keys=True,indent=2)+"\n")

def mirror_symlink(link,target):
    try:
        if target is None: link.unlink(missing_ok=True)
        else: replace_symlink(link,target)
    except OSError as error:
        print(f"devc2: warning: compatibility pointer {link} could not be updated: {error}",file=sys.stderr)

def wrapper_root(wrapper):
    import re
    try: text=wrapper.read_text(encoding="utf-8")
    except OSError: return None
    match=re.search(r"^export (DEVC2_WRAPPER_ROOT|DEVC2_SHARE_DIR|DEVC2_DATA_DIR)=(.+)$",text,re.MULTILINE)
    if not match: return None
    try:
        fields=shlex.split(match.group(2))
        root=Path(fields[0]).expanduser()
        if match.group(1)=="DEVC2_DATA_DIR": root=root/"current"
        return root.resolve() if len(fields)==1 else None
    except ValueError: return None

def install_wrapper_text(data_root,bin_dir,runtime_root):
    dispatcher=runtime_root/"launcher"/"dispatcher.py"
    return "#!/usr/bin/env bash\n"+f"export DEVC2_WRAPPER_ROOT={shlex.quote(str(runtime_root))}\n"+f"export DEVC2_DATA_DIR={shlex.quote(str(data_root))}\n"+f"export DEVC2_BIN_DIR={shlex.quote(str(bin_dir))}\n"+f"exec python3 {shlex.quote(str(dispatcher))} \"$@\"\n"

def copy_release_assets(source,destination):
    destination.mkdir(mode=0o700)
    for name in ASSET_ROOTS:
        origin=source/name
        if not origin.exists(): continue
        target=destination/name
        if origin.is_dir(): shutil.copytree(origin,target,symlinks=False,ignore=shutil.ignore_patterns("__pycache__","tests"))
        else: shutil.copyfile(origin,target)
    for path in destination.rglob("*"):
        if path.is_dir(): path.chmod(0o755)
        elif path.is_file(): path.chmod(0o644)
    for relative in EXECUTABLE_ASSETS:
        (destination/relative).chmod(0o755)

def freeze_asset_tree(destination):
    for path in destination.rglob("*"):
        if path.is_dir(): path.chmod(0o555)
        elif path.is_file(): path.chmod(0o444)
    for relative in EXECUTABLE_ASSETS:
        (destination/relative).chmod(0o555)
    destination.chmod(0o555)

def remove_staging(path):
    if not path.exists() or path.is_symlink(): return
    for item in path.rglob("*"):
        try: item.chmod(0o700 if item.is_dir() else 0o600)
        except OSError: pass
    try: path.chmod(0o700)
    except OSError: pass
    shutil.rmtree(path,ignore_errors=True)

def ensure_owned_directory(path):
    path.mkdir(parents=True,exist_ok=True)
    metadata=path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode): raise RuntimeError(f"installation path must be a real directory: {path}")
    if metadata.st_uid!=os.getuid() or metadata.st_mode & 0o022: raise RuntimeError(f"installation path must be owned by the current user and not group/world writable: {path}")

def replace_symlink(link,target):
    temporary=link.parent/f".{link.name}.{secrets.token_hex(8)}"
    os.symlink(str(target),temporary)
    try:
        os.replace(temporary,link)
        directory=os.open(link.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally: temporary.unlink(missing_ok=True)

def installed_version(root):
    """Return a verified immutable install ID, or the embedded base version."""
    base=source_version(root)
    if root.parent.name=="versions":
        local=f"{base}+local.{asset_digest(root)[:12]}"
        if root.name in {base,local}: return root.name
    return base

def prune_unreferenced_versions(share_dir):
    """Remove verified generations not named by the committed schema-2 record."""
    state=read_installation_state(share_dir)
    if state.get("schema")!=2: return
    referenced={Path(state[name]) for name in ("current","previous","manager") if state.get(name)}
    versions=share_dir/"versions"
    # The wrapper replacement deliberately follows the record commit. Preserve
    # its immutable dispatcher generation across interrupted activations until
    # a later successful activation advances the wrapper.
    wrapper=wrapper_root(Path(state["bin_dir"])/"devc2")
    if wrapper and wrapper.parent==versions:
        try: validate_asset_tree(wrapper,True)
        except (OSError,RuntimeError): pass
        else: referenced.add(wrapper)
    try: candidates=tuple(versions.iterdir())
    except FileNotFoundError: return
    for candidate in candidates:
        if candidate in referenced: continue
        try:
            metadata=candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode): continue
            base=validate_asset_tree(candidate,True)
            local=f"{base}+local.{asset_digest(candidate)[:12]}"
            if candidate.name not in {base,local}: continue
        except (OSError,RuntimeError): continue
        remove_staging(candidate)

def _activate_assets(source,bin_dir,share_dir,release=False):
    version=validate_asset_tree(source)
    bin_dir=bin_dir.expanduser().absolute(); share_dir=share_dir.expanduser().absolute()
    ensure_owned_directory(bin_dir); ensure_owned_directory(share_dir)
    versions=share_dir/"versions"; ensure_owned_directory(versions)
    digest=asset_digest(source)
    install_id=version if release else f"{version}+local.{digest[:12]}"
    target=versions/install_id; launcher_path=bin_dir/"devc2"
    current_link=share_dir/"current"; previous_link=share_dir/"previous"
    existing_state=read_installation_state(share_dir)
    if existing_state.get("schema")==2:
        old_root=Path(existing_state["current"])
    else:
        try: old_root=current_link.resolve(strict=True) if current_link.is_symlink() else wrapper_root(launcher_path)
        except OSError: old_root=wrapper_root(launcher_path)
    if not target.exists():
        staging=Path(tempfile.mkdtemp(prefix=f".{install_id}.",dir=versions)); staging.rmdir()
        try:
            copy_release_assets(source,staging)
            if validate_asset_tree(staging,True)!=version or asset_digest(staging)!=digest:
                raise RuntimeError("source assets changed while the runtime snapshot was staged")
            freeze_asset_tree(staging)
            staging.rename(target)
        except Exception:
            if staging.exists(): remove_staging(staging)
            raise
    elif validate_asset_tree(target,True)!=version or asset_digest(target)!=digest:
        raise RuntimeError(f"existing installation target failed immutable content validation: {target}")
    # The wrapper names an immutable dispatcher. Commit the record first: an old
    # wrapper can read the new schema, and a new wrapper can read the old schema,
    # so interruption on either side of the commit always leaves a usable pair.
    expected_wrapper=install_wrapper_text(share_dir,bin_dir,target)
    if old_root==target:
        raw_previous=existing_state.get("previous")
        if raw_previous: previous_root=Path(raw_previous)
        else:
            try: previous_root=previous_link.resolve(strict=True) if previous_link.is_symlink() else None
            except OSError: previous_root=None
    else: previous_root=old_root
    previous_version=None
    if previous_root:
        try: previous_version=installed_version(previous_root)
        except RuntimeError: pass
    if old_root==target:
        previous_install_kind=existing_state.get("previous_install_kind")
        previous_source_dir=existing_state.get("previous_source_dir")
    elif old_root and existing_state.get("current")==str(old_root):
        previous_install_kind=existing_state.get("install_kind")
        previous_source_dir=existing_state.get("source_dir")
    else:
        previous_install_kind=None; previous_source_dir=None
    metadata={"schema":2,"version":install_id,"current":str(target),"previous":str(previous_root) if previous_root else None,"manager":str(target),"previous_version":previous_version,"bin_dir":str(bin_dir),"share_dir":str(share_dir),"install_kind":"release" if release else "checkout","source_dir":None if release else str(source.resolve()),"previous_install_kind":previous_install_kind,"previous_source_dir":previous_source_dir,"installed_at":int(time.time())}
    write_installation_record(share_dir,metadata)
    # A legacy or prior-generation wrapper remains usable after the record
    # commit. Replacing it now only advances the immutable dispatcher it uses.
    if not launcher_path.exists() or launcher_path.read_text(encoding="utf-8")!=expected_wrapper:
        atomic_write(launcher_path,expected_wrapper,0o555)
    mirror_symlink(share_dir/"manager",target)
    mirror_symlink(previous_link,previous_root)
    mirror_symlink(current_link,target)
    print(f"installed devc2 {install_id}: {launcher_path}")
    print(f"installed devc2 assets: {target}")
    if previous_root: print(f"rollback preserved: {previous_root}")
    if str(bin_dir) not in os.environ.get("PATH","").split(os.pathsep): print(f"note: add {bin_dir} to PATH")
    return 0

def install_from_directory(source,bin_dir,share_dir,release=False):
    with lifecycle_lock(True):
        active=active_legacy_sessions()
        if active: raise RuntimeError("cannot install while a devc2 session is active")
        result=_activate_assets(source.resolve(),bin_dir,share_dir,release)
        prune_unreferenced_versions(share_dir.expanduser().absolute())
        return result

def parse_release_version(tag):
    import re
    match=re.fullmatch(r"devc2-v([0-9]+\.[0-9]+\.[0-9]+)",tag or "")
    if not match: raise RuntimeError(f"latest release has an invalid tag: {tag!r}")
    return match.group(1)

def version_tuple(value): return tuple(int(part) for part in value.split("."))

TRUSTED_RELEASE_HOSTS={"api.github.com","github.com","release-assets.githubusercontent.com","objects.githubusercontent.com"}

def trusted_https_url(url):
    parsed=urllib.parse.urlparse(url)
    return parsed.scheme=="https" and parsed.hostname in TRUSTED_RELEASE_HOSTS and parsed.username is None and parsed.password is None

class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        if not trusted_https_url(newurl): raise RuntimeError("release download attempted an untrusted redirect")
        return super().redirect_request(request,fp,code,msg,headers,newurl)

def read_https(request,max_bytes):
    if not trusted_https_url(request.full_url): raise RuntimeError("release URL is not trusted GitHub HTTPS")
    opener=urllib.request.build_opener(TrustedRedirectHandler())
    try:
        with opener.open(request,timeout=30) as response:
            if not trusted_https_url(response.geturl()): raise RuntimeError("release download redirected outside trusted GitHub HTTPS hosts")
            length=response.headers.get("Content-Length")
            if length and int(length)>max_bytes: raise RuntimeError("release download exceeds the size limit")
            payload=response.read(max_bytes+1)
    except RuntimeError: raise
    except (OSError,urllib.error.HTTPError,urllib.error.URLError,ValueError) as error: raise RuntimeError("could not download the devc2 release") from error
    if len(payload)>max_bytes: raise RuntimeError("release download exceeds the size limit")
    return payload

def latest_release():
    request=urllib.request.Request(RELEASE_API,headers={"Accept":"application/vnd.github+json","User-Agent":f"devc2/{VERSION}","X-GitHub-Api-Version":"2022-11-28"})
    try: document=json.loads(read_https(request,2*1024*1024))
    except ValueError as error: raise RuntimeError("GitHub returned invalid release metadata") from error
    version=parse_release_version(document.get("tag_name"))
    matches=[asset for asset in document.get("assets",[]) if isinstance(asset,dict) and asset.get("name")==RELEASE_ASSET]
    if len(matches)!=1: raise RuntimeError(f"release {version} must contain exactly one {RELEASE_ASSET}")
    asset=matches[0]; digest=asset.get("digest")
    import re
    if not isinstance(digest,str) or not re.fullmatch(r"sha256:[0-9a-f]{64}",digest):
        raise RuntimeError("release asset lacks its GitHub SHA-256 digest")
    url=asset.get("browser_download_url")
    if not isinstance(url,str) or not url.startswith("https://github.com/"):
        raise RuntimeError("release asset has an invalid download URL")
    return version,url,digest[7:]

@contextlib.contextmanager
def bounded_release_tar(archive):
    descriptor,name=tempfile.mkstemp(prefix="devc2-release-",suffix=".tar")
    output_path=Path(name); total=0
    try:
        with os.fdopen(descriptor,"wb") as output, gzip.open(archive,"rb") as compressed:
            while True:
                chunk=compressed.read(1024*1024)
                if not chunk: break
                total+=len(chunk)
                if total>MAX_RELEASE_TAR_BYTES: raise RuntimeError("release archive expands beyond the size limit")
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        yield output_path
    except (OSError,gzip.BadGzipFile,EOFError) as error:
        raise RuntimeError("release archive is not valid bounded gzip data") from error
    finally: output_path.unlink(missing_ok=True)

def _validated_archive_members(archive):
    members=[]; total=0; names=set()
    with tarfile.open(archive,"r:") as package:
        while True:
            member=package.next()
            if member is None: break
            if len(members)>=256: raise RuntimeError("release archive contains too many entries")
            raw=member.name.rstrip("/") if member.isdir() else member.name
            components=raw.split("/")
            path=PurePosixPath(raw)
            normalized=unicodedata.normalize("NFC",raw).casefold()
            if not raw or "\\" in raw or any(part in {"",".",".."} for part in components) or path.is_absolute() or components[0]!="devc2":
                raise RuntimeError("release archive contains an unsafe path")
            if normalized in names: raise RuntimeError("release archive contains duplicate or case-colliding paths")
            names.add(normalized)
            if not (member.isdir() or member.isfile()) or any(key.startswith("GNU.sparse") for key in member.pax_headers):
                raise RuntimeError("release archive contains links, sparse entries, or special files")
            total+=member.size
            if member.size>MAX_RELEASE_BYTES or total>MAX_RELEASE_BYTES: raise RuntimeError("release archive exceeds the size limit")
            members.append((member.name,member.size,member.isdir()))
    return members

def extract_release(archive,destination):
    with bounded_release_tar(archive) as tar_path:
        members=_validated_archive_members(tar_path)
        destination.mkdir(mode=0o700)
        with tarfile.open(tar_path,"r:") as package:
            for expected_name,expected_size,is_directory in members:
                member=package.next()
                if member is None or member.name!=expected_name or member.size!=expected_size or member.isdir()!=is_directory:
                    raise RuntimeError("release archive changed during validation")
                raw=member.name.rstrip("/") if member.isdir() else member.name
                target=destination.joinpath(*raw.split("/"))
                if member.isdir(): target.mkdir(parents=True,exist_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    source=package.extractfile(member)
                    if source is None: raise RuntimeError("release archive contains an unreadable file")
                    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
                    written=0
                    with os.fdopen(descriptor,"wb") as output:
                        while True:
                            chunk=source.read(min(1024*1024,expected_size-written+1))
                            if not chunk: break
                            written+=len(chunk)
                            if written>expected_size: raise RuntimeError("release archive member exceeds its declared size")
                            output.write(chunk)
                    if written!=expected_size: raise RuntimeError("release archive member is truncated")
    source=destination/"devc2"
    validate_asset_tree(source,True)
    return source

def promoted_checkout_hint(source):
    if source.name!="v2": return ""
    candidate=source.parent
    try:
        candidate=candidate.resolve(strict=True)
        validate_asset_tree(candidate)
    except (OSError,RuntimeError): return ""
    return f"; source layout changed: run {candidate/'install.sh'} once"

def update_installation():
    with lifecycle_lock(False), forward_update_lock():
        data_root=Path(os.environ.get("DEVC2_DATA_DIR",Path.home()/".local/share/devc2"))
        state=read_installation_state(data_root)
        if state.get("install_kind")=="checkout":
            raw_source=state.get("source_dir")
            if not isinstance(raw_source,str) or not raw_source:
                raise RuntimeError("checkout installation metadata lacks its source directory; rerun ./install.sh once")
            source=Path(raw_source).expanduser()
            try: source=source.resolve(strict=True)
            except OSError as error:
                hint=promoted_checkout_hint(source)
                raise RuntimeError(f"installed checkout source is unavailable: {source}{hint}") from error
            try: validate_asset_tree(source)
            except RuntimeError as error:
                hint=promoted_checkout_hint(source)
                if hint: raise RuntimeError(f"installed checkout source is invalid: {source}{hint}") from error
                raise
            bin_dir=Path(os.environ.get("DEVC2_BIN_DIR",state.get("bin_dir",Path.home()/".local/bin")))
            share_dir=Path(os.environ.get("DEVC2_DATA_DIR",state.get("share_dir",Path.home()/".local/share/devc2")))
            return _activate_assets(source,bin_dir,share_dir,False)
        latest,url,expected=latest_release()
        try:
            active=Path(state["current"]) if state.get("schema")==2 else (data_root/"current").resolve(strict=True)
            active_version=source_version(active)
        except (RuntimeError,OSError): active_version=VERSION
        if version_tuple(latest)<=version_tuple(active_version):
            print(f"devc2 {active_version} is current")
            return 0
        request=urllib.request.Request(url,headers={"Accept":"application/octet-stream","User-Agent":f"devc2/{VERSION}"})
        payload=read_https(request,MAX_RELEASE_BYTES)
        actual=hashlib.sha256(payload).hexdigest()
        if not secrets.compare_digest(actual,expected): raise RuntimeError("release asset SHA-256 digest does not match GitHub metadata")
        with tempfile.TemporaryDirectory(prefix="devc2-update-") as raw:
            temp=Path(raw); archive=temp/RELEASE_ASSET; archive.write_bytes(payload)
            source=extract_release(archive,temp/"extracted")
            if source_version(source)!=latest: raise RuntimeError("release tag and packaged VERSION do not match")
            bin_dir=Path(os.environ.get("DEVC2_BIN_DIR",state.get("bin_dir",Path.home()/".local/bin")))
            share_dir=Path(os.environ.get("DEVC2_DATA_DIR",state.get("share_dir",Path.home()/".local/share/devc2")))
            return _activate_assets(source,bin_dir,share_dir,True)

def rollback_installation():
    with lifecycle_lock(True):
        if active_legacy_sessions(): raise RuntimeError("cannot roll back while a devc2 session is active")
        requested_share=Path(os.environ.get("DEVC2_DATA_DIR",Path.home()/".local/share/devc2"))
        state=read_installation_state(requested_share); share_dir=Path(os.environ.get("DEVC2_DATA_DIR",state.get("share_dir",requested_share))).expanduser().resolve(); bin_dir=Path(os.environ.get("DEVC2_BIN_DIR",state.get("bin_dir",Path.home()/".local/bin"))).expanduser().resolve()
        current_link=share_dir/"current"; previous_link=share_dir/"previous"
        if state.get("schema")==2:
            current=Path(state["current"]); raw_previous=state.get("previous")
            if not raw_previous: raise RuntimeError("no previous devc2 installation is available")
            previous=Path(raw_previous); manager=Path(state["manager"])
        else:
            if not current_link.is_symlink() or not previous_link.is_symlink(): raise RuntimeError("no previous devc2 installation is available")
            try: current=current_link.resolve(strict=True); previous=previous_link.resolve(strict=True)
            except OSError as error: raise RuntimeError("saved rollback installation is unavailable") from error
            manager=current
        if not current.is_dir() or current.is_symlink() or not previous.is_dir() or previous.is_symlink():
            raise RuntimeError("saved rollback installation is unavailable")
        validate_asset_tree(previous,previous.parent.name=="versions")
        previous_version=installed_version(previous)
        current_version=installed_version(current)
        metadata={"schema":2,"version":previous_version,"current":str(previous),"previous":str(current),"manager":str(manager),"previous_version":current_version,"bin_dir":str(bin_dir),"share_dir":str(share_dir),"install_kind":state.get("previous_install_kind") or "legacy","source_dir":state.get("previous_source_dir"),"previous_install_kind":state.get("install_kind") or "legacy","previous_source_dir":state.get("source_dir"),"installed_at":int(time.time())}
        write_installation_record(share_dir,metadata)
        mirror_symlink(current_link,previous)
        mirror_symlink(previous_link,current)
        mirror_symlink(share_dir/"manager",manager)
        prune_unreferenced_versions(share_dir)
        print(f"rolled back devc2 to {metadata['version']}")
        return 0

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

def canonical_checkout(raw):
    path = Path(raw).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise RuntimeError(f"checkout is not a directory: {path}")
    if not ((path / ".git").exists() or (path / ".jj").exists()):
        raise RuntimeError(f"path must be a Git or Jujutsu checkout: {path}")
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
            raise RuntimeError(f"credential directory would be exposed by checkout mount: {secret_root}")
    return path

def git_admin_text(path,label):
    try:
        metadata=path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size>4096:
            raise RuntimeError
        value=path.read_text(encoding="utf-8").strip()
    except (OSError,UnicodeError,RuntimeError) as error:
        raise RuntimeError(f"linked worktree has an invalid {label}: {path}") from error
    if not value or "\0" in value or "\n" in value or "\r" in value:
        raise RuntimeError(f"linked worktree has an invalid {label}: {path}")
    return value

def resolved_git_admin_path(raw,base,label):
    candidate=Path(raw)
    if not candidate.is_absolute(): candidate=base/candidate
    try: return candidate.resolve(strict=True)
    except OSError as error: raise RuntimeError(f"linked worktree {label} is unavailable: {candidate}") from error

def lexical_git_admin_path(raw,base):
    candidate=Path(raw)
    if not candidate.is_absolute(): candidate=base/candidate
    return Path(os.path.normpath(candidate))

def validate_git_mount_target(path):
    reserved=(Path("/workspace"),Path("/home/devbox"),Path("/run"),Path("/proc"),Path("/sys"),Path("/dev"),Path("/etc"),Path("/usr"),Path("/bin"),Path("/sbin"),Path("/lib"),Path("/lib64"))
    if not path.is_absolute() or any(path==item or path.is_relative_to(item) or item.is_relative_to(path) for item in reserved):
        raise RuntimeError(f"linked worktree Git metadata targets a reserved Box path: {path}")

def validate_git_object_store(common):
    objects=common/"objects"
    if not objects.is_dir() or objects.is_symlink():
        raise RuntimeError("linked worktree uses an unsupported Git object store")
    alternates=objects/"info"/"alternates"
    if not alternates.exists(): return
    for raw in git_admin_text(alternates,"objects/info/alternates").splitlines():
        target=resolved_git_admin_path(raw,objects,"alternate Git object store")
        if not target.is_relative_to(common):
            raise RuntimeError(f"linked worktree uses an external Git object store: {target}")

class WorktreeProjection(NamedTuple):
    pointer: Path
    backlink_target: Path
    gitdir: Path
    gitdir_target: Path
    common: Path
    common_target: Path

def linked_worktree(repo):
    pointer=repo/".git"
    try: pointer_metadata=pointer.lstat()
    except FileNotFoundError: return None
    if stat.S_ISLNK(pointer_metadata.st_mode):
        raise RuntimeError(f"Git metadata must not be a symlink: {pointer}")
    if stat.S_ISDIR(pointer_metadata.st_mode): return None
    line=git_admin_text(pointer,".git pointer")
    if not line.startswith("gitdir: "):
        raise RuntimeError(f"unsupported Git metadata file: {pointer}")
    gitdir_raw=line[8:]
    gitdir=resolved_git_admin_path(gitdir_raw,repo,"Git directory")
    gitdir_target=lexical_git_admin_path(gitdir_raw,Path("/workspace"))
    if not gitdir.is_dir(): raise RuntimeError(f"linked worktree Git directory is invalid: {gitdir}")
    git_admin_text(gitdir/"HEAD","worktree HEAD")
    commondir_raw=git_admin_text(gitdir/"commondir","commondir")
    common=resolved_git_admin_path(
        commondir_raw,gitdir,"common Git directory",
    )
    common_target=lexical_git_admin_path(commondir_raw,gitdir_target)
    backlink_raw=git_admin_text(gitdir/"gitdir","gitdir backlink")
    backlink=resolved_git_admin_path(
        backlink_raw,gitdir,"gitdir backlink",
    )
    backlink_target=lexical_git_admin_path(backlink_raw,gitdir_target)
    if (common.name!=".git" or gitdir.parent!=common/"worktrees"
            or gitdir_target.parent!=common_target/"worktrees"
            or backlink!=pointer.resolve(strict=True)):
        raise RuntimeError("linked worktree metadata does not form a closed Git relationship")
    validate_git_mount_target(common_target)
    validate_git_mount_target(backlink_target)
    git_admin_text(common/"HEAD","common HEAD")
    validate_git_object_store(common)
    home=Path.home().resolve()
    main_checkout=common.parent
    if main_checkout==home or home.is_relative_to(main_checkout):
        raise RuntimeError("refusing linked worktree metadata rooted at the host home or one of its parents")
    for secret_root in (
        Path(os.environ.get("CODEX_HOME",home/".codex")).expanduser().resolve(),
        (home/".config"/"gh").resolve(), CONFIG.resolve(),
    ):
        if secret_root==common or secret_root.is_relative_to(common) or common.is_relative_to(secret_root):
            raise RuntimeError(f"credential directory would be exposed by Git metadata mount: {secret_root}")
    return WorktreeProjection(
        pointer.resolve(strict=True),backlink_target,gitdir,gitdir_target,common,common_target,
    )

def identity(repo):
    digest=hashlib.sha256(os.fsencode(repo)).hexdigest()[:16]
    return digest, f"devc2-{digest}"

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

def _skill_manifest(root):
    root_metadata=root.stat()
    if (not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid!=os.getuid()
            or root_metadata.st_mode&0o022):
        raise RuntimeError(f"agent skills root must be user-owned and not group/world-writable: {root}")
    skill_name=re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
    skills=[]; root_entries=0
    with os.scandir(root) as candidates:
        for candidate in candidates:
            root_entries+=1
            if root_entries>128: raise RuntimeError("agent skills root contains more than 128 entries")
            if candidate.name.startswith("."): continue
            if candidate.is_file(follow_symlinks=False): continue
            if not candidate.is_dir(follow_symlinks=False):
                raise RuntimeError(f"agent skills root contains an unsafe path: {candidate.name}")
            if len(candidate.name)>64 or not skill_name.fullmatch(candidate.name):
                raise RuntimeError(f"invalid agent skill directory name: {candidate.name!r}")
            skills.append(Path(candidate.path))
    if len(skills)>64: raise RuntimeError("agent skills root contains more than 64 skills")
    entries=[]; total=0
    def inspect(path,depth):
        nonlocal total
        relative=path.relative_to(root)
        metadata=path.lstat()
        if (metadata.st_uid!=os.getuid() or metadata.st_mode&0o022
                or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))):
            raise RuntimeError(f"agent skill contains an unsafe path: {relative}")
        digest=None
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink!=1: raise RuntimeError(f"agent skill contains a hard-linked file: {relative}")
            if metadata.st_size>MAX_SKILL_FILE_BYTES:
                raise RuntimeError(f"agent skill file exceeds 4 MiB: {relative}")
            with path.open("rb") as source: payload=source.read(MAX_SKILL_FILE_BYTES+1)
            if len(payload)>MAX_SKILL_FILE_BYTES:
                raise RuntimeError(f"agent skill file exceeds 4 MiB: {relative}")
            if len(payload)!=metadata.st_size:
                raise RuntimeError(f"agent skill changed while being inspected: {relative}")
            total+=len(payload)
            if total>MAX_SKILL_BYTES: raise RuntimeError("agent skills exceed the 16 MiB size limit")
            digest=hashlib.sha256(payload).hexdigest()
        entries.append((relative,stat.S_ISDIR(metadata.st_mode),bool(metadata.st_mode&0o111),metadata.st_size,digest))
        if len(entries)>MAX_SKILL_FILES: raise RuntimeError("agent skills contain more than 512 paths")
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as children:
                for child in children:
                    if depth>=8: raise RuntimeError(f"agent skill path is too deep: {relative/child.name}")
                    inspect(Path(child.path),depth+1)
    for skill in sorted(skills,key=lambda item:item.name):
        first=len(entries); inspect(skill,0)
        manifest=skill.relative_to(root)/"SKILL.md"
        if not any(relative==manifest and not is_directory for relative,is_directory,*_rest in entries[first:]):
            raise RuntimeError(f"agent skill lacks SKILL.md: {skill.name}")
    entries.sort(key=lambda item:str(item[0]))
    return entries

def snapshot_agent_skills(source,destination,*,append=False,reserved=frozenset()):
    if append:
        metadata=destination.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
            raise RuntimeError("agent skill snapshot is unsafe")
        destination.chmod(0o700)
    else:
        destination.mkdir(mode=0o700)
    if not source.exists() and not source.is_symlink():
        destination.chmod(0o555)
        return []
    try: root=source.resolve(strict=True)
    except OSError as error: raise RuntimeError(f"agent skills root is unavailable: {source}") from error
    before=_skill_manifest(root)
    selected=[entry for entry in before if entry[0].parts[0] not in reserved]
    for relative,is_directory,executable,size,digest in selected:
        target=destination/relative
        if is_directory:
            target.mkdir(mode=0o700)
            continue
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
        descriptor=os.open(root/relative,flags)
        try:
            metadata=os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size!=size:
                raise RuntimeError(f"agent skill changed while being copied: {relative}")
            with os.fdopen(descriptor,"rb") as input_file:
                descriptor=None; payload=input_file.read(MAX_SKILL_FILE_BYTES+1)
            if len(payload)!=size or hashlib.sha256(payload).hexdigest()!=digest:
                raise RuntimeError(f"agent skill changed while being copied: {relative}")
            target.write_bytes(payload)
            target.chmod(0o555 if executable else 0o444)
        finally:
            if descriptor is not None: os.close(descriptor)
    if _skill_manifest(root)!=before: raise RuntimeError("agent skills changed while the snapshot was created")
    for path in sorted((item for item in destination.rglob("*") if item.is_dir()),reverse=True): path.chmod(0o555)
    destination.chmod(0o555)
    return sorted({relative.parts[0] for relative,*_rest in selected})

def _publish_skill_view(snapshot,view):
    view.mkdir(mode=0o700,exist_ok=True)
    metadata=view.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
        raise RuntimeError("agent skill view is unsafe")
    existing=[]
    for path in view.rglob("*"):
        metadata=path.lstat(); relative=path.relative_to(view)
        if (metadata.st_uid!=os.getuid() or metadata.st_mode&0o022
                or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
                or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink!=1)):
            raise RuntimeError(f"agent skill view contains an unsafe path: {relative}")
        existing.append((relative,stat.S_ISDIR(metadata.st_mode)))
    desired={}
    for path in snapshot.rglob("*"):
        metadata=path.lstat(); relative=path.relative_to(snapshot)
        desired[relative]=(stat.S_ISDIR(metadata.st_mode),bool(metadata.st_mode&0o111))
    for relative,(is_directory,_executable) in sorted(desired.items(),key=lambda item:len(item[0].parts)):
        if not is_directory: continue
        target=view/relative
        if target.is_file(): target.unlink()
        target.mkdir(mode=0o700,parents=True,exist_ok=True)
        target.chmod(0o700)
    for relative,(is_directory,executable) in desired.items():
        if is_directory: continue
        target=view/relative
        if target.is_dir():
            for directory,_children,_files in os.walk(target,topdown=False): Path(directory).chmod(0o700)
            shutil.rmtree(target)
        atomic_write(target,(snapshot/relative).read_bytes(),0o555 if executable else 0o444)
    for relative,is_directory in sorted(existing,key=lambda item:len(item[0].parts),reverse=True):
        if relative in desired: continue
        target=view/relative
        if is_directory:
            target.chmod(0o700); target.rmdir()
        else: target.unlink()

def update_skill_projection(source,projection):
    projection.mkdir(mode=0o700,exist_ok=True)
    metadata=projection.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
        raise RuntimeError("agent skill projection is unsafe")
    snapshots=projection/"snapshots"; snapshots.mkdir(mode=0o700,exist_ok=True)
    metadata=snapshots.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
        raise RuntimeError("agent skill projection is unsafe")
    name=f"snapshot-{secrets.token_hex(16)}"
    snapshot=snapshots/name
    current=projection/"current"
    try:
        if not BUILTIN_AGENT_SKILLS.is_dir():
            raise RuntimeError("bundled agent skills are unavailable")
        bundled=snapshot_agent_skills(BUILTIN_AGENT_SKILLS,snapshot)
        host=snapshot_agent_skills(
            source,snapshot,append=True,reserved=frozenset(bundled),
        )
        skills=sorted(bundled+host)
        _publish_skill_view(snapshot,current)
        atomic_write(current/".devc2-generation",name+"\n",0o444)
        directory=os.open(projection,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except Exception:
        if snapshot.exists():
            try: _remove_skill_snapshot(snapshot)
            except (OSError,RuntimeError): pass
        raise
    return skills

def _remove_skill_snapshot(path):
    metadata=path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("agent skill projection contains an unsafe snapshot")
    for directory,_children,_files in os.walk(path,topdown=False,followlinks=False):
        current=Path(directory)
        if any(item.is_symlink() for item in current.iterdir()):
            raise RuntimeError("agent skill projection contains an unsafe snapshot")
        current.chmod(0o700)
    shutil.rmtree(path)

def _running_skill_projection(devbox):
    try:
        mounts=json.loads(run([
            "docker","inspect","--format","{{json .Mounts}}",devbox,
        ],timeout=15).stdout)
    except (ValueError,TypeError) as error:
        raise RuntimeError("running Box has invalid mount metadata") from error
    if not isinstance(mounts,list):
        raise RuntimeError("running Box has invalid mount metadata")
    by_target={}
    for mount in mounts:
        if not isinstance(mount,dict) or not isinstance(mount.get("Destination"),str): continue
        by_target.setdefault(mount["Destination"],[]).append(mount)
    def host_source(raw,target):
        candidates=[Path(raw)]
        parsed=PurePosixPath(raw)
        if (platform.system()=="Darwin" and parsed.is_absolute()
                and len(parsed.parts)>2 and parsed.parts[1]=="host_mnt"):
            candidates.append(Path("/").joinpath(*parsed.parts[2:]))
        for candidate in candidates:
            try: return candidate.resolve(strict=True)
            except OSError: pass
        raise RuntimeError(f"running Box mount is unavailable: {target}")
    def read_only_bind(target):
        matches=by_target.get(target,[])
        if (len(matches)!=1 or matches[0].get("Type")!="bind" or matches[0].get("RW") is not False
                or not isinstance(matches[0].get("Source"),str)):
            raise RuntimeError(f"running Box lacks its expected read-only mount: {target}")
        return host_source(matches[0]["Source"],target)
    public=read_only_bind("/run/devc2-public")
    projection=public/"agent-skills"
    agent_home=read_only_bind("/home/devbox/.agents")
    if agent_home!=public/"agent-home":
        raise RuntimeError("running Box has an invalid agent skills view")
    for path in (public,agent_home,projection,projection/"snapshots"):
        try: metadata=path.lstat()
        except OSError as error: raise RuntimeError("running Box has an unsafe agent skills projection") from error
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
            raise RuntimeError("running Box has an unsafe agent skills projection")
    skills_view=agent_home/"skills"
    if (not skills_view.is_symlink()
            or os.readlink(skills_view)!="/run/devc2-public/agent-skills/current"):
        raise RuntimeError("running Box has an invalid agent skills view")
    current=projection/"current"
    try: metadata=current.lstat()
    except OSError as error: raise RuntimeError("running Box has an invalid agent skills projection") from error
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
        raise RuntimeError("running Box has an invalid agent skills projection")
    for snapshot in (projection/"snapshots").iterdir():
        metadata=snapshot.lstat()
        if (not re.fullmatch(r"snapshot-[a-f0-9]{32}",snapshot.name)
                or not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022):
            raise RuntimeError("running Box has an unsafe agent skills projection")
    return projection

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
            raise RuntimeError("ChatGPT auth must be a regular file smaller than 1 MiB")
        with os.fdopen(descriptor, "r", encoding="utf-8") as auth_file:
            doc = json.load(auth_file)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"valid ChatGPT login required at {path}; run `devc2 auth`") from e
    tokens=doc.get("tokens",doc)
    access=tokens.get("access_token"); account=tokens.get("account_id")
    if not access or not account: raise RuntimeError(f"ChatGPT login at {path} lacks access_token/account_id")
    try:
        body=access.split('.')[1]; body+='='*(-len(body)%4); exp=json.loads(base64.urlsafe_b64decode(body))["exp"]
        if exp < time.time()+3600: raise RuntimeError("ChatGPT access token expires within one hour; run `devc2 auth`")
    except RuntimeError: raise
    except Exception as e: raise RuntimeError("ChatGPT access token is malformed") from e
    return access,account

def bound_image_id(image):
    inspected=run(["docker","image","inspect","--format","{{json .}}",image],check=False,timeout=15)
    if inspected.returncode: return None
    try:
        document=json.loads(inspected.stdout)
        image_id=document["Id"]
        labels=document.get("Config",{}).get("Labels") or {}
    except (TypeError,ValueError,KeyError): return None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}",image_id) or labels.get(IMAGE_LABEL)!=ASSET_DIGEST:
        return None
    return image_id

def require_bound_image(image):
    image_id=bound_image_id(image)
    if image_id is None: raise RuntimeError(f"Docker image failed devc2 content-identity verification: {image}")
    return image_id

def ensure_auth_image():
    image_id=bound_image_id(AUTH_IMAGE)
    if image_id is not None: return image_id
    print(f"Building local devc2 authentication image {AUTH_IMAGE}…")
    run(["docker","build","--target","auth","--label",f"{IMAGE_LABEL}={ASSET_DIGEST}","--tag",AUTH_IMAGE,str(ROOT)],capture=False,timeout=1800)
    return require_bound_image(AUTH_IMAGE)

def ensure_runtime_image():
    image_id=bound_image_id(RUNTIME_IMAGE)
    if image_id is not None: return image_id
    print(f"Building local devc2 runtime image {RUNTIME_IMAGE}…")
    run(["docker","build","--label",f"{IMAGE_LABEL}={ASSET_DIGEST}","--tag",RUNTIME_IMAGE,str(ROOT)],capture=False,timeout=1800)
    return require_bound_image(RUNTIME_IMAGE)

def github_token():
    managed = AUTH / "gh"
    if (managed / "hosts.yml").exists():
        runtime_image_id=ensure_runtime_image()
        value = run([
            "docker", "run", "--rm", "--pull=never", "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp", "--env", "GH_CONFIG_DIR=/run/devc2-gh",
            "--env", "GH_TOKEN=", "--env", "GITHUB_TOKEN=",
            "--volume", f"{managed}:/run/devc2-gh:ro",
            "--entrypoint", "gh", runtime_image_id, "auth", "token",
        ], timeout=30).stdout.strip()
    else:
        try:
            value = run(["gh", "auth", "token"], timeout=15).stdout.strip()
        except RuntimeError as error:
            raise RuntimeError("not configured; run `devc2 auth`") from error
    if not value or any(c.isspace() for c in value):
        raise RuntimeError("GitHub login is invalid; run `devc2 auth`")
    return value


def host_github_token():
    try:
        value = run(["gh", "auth", "token"], timeout=15).stdout.strip()
    except RuntimeError as error:
        raise RuntimeError("host GitHub CLI is not authenticated") from error
    if not value or any(c.isspace() for c in value):
        raise RuntimeError("host GitHub CLI returned an invalid token")
    return value


def validate_github_token(token=None):
    token = token or github_token()
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
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError("credential was rejected by GitHub; run `devc2 auth`") from error
        raise RuntimeError(f"GitHub API returned HTTP {error.code}; retry later") from error
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError("GitHub API is unavailable; retry later") from error
    except ValueError as error:
        raise RuntimeError("GitHub returned an invalid authenticated identity") from error
    login = document.get("login")
    if not isinstance(login, str) or not login:
        raise RuntimeError("GitHub returned an invalid authenticated identity")
    return login


def import_host_github_auth():
    token = host_github_token()
    login = validate_github_token(token)
    if (not re.fullmatch(r"[A-Za-z0-9_-]+", token)
            or not re.fullmatch(r"[A-Za-z0-9-]+", login)):
        raise RuntimeError("host GitHub CLI returned an unsupported credential")
    directory = AUTH / "gh"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = directory / (".hosts.yml." + secrets.token_hex(8))
    try:
        private_write(temporary, (
            "github.com:\n"
            "  git_protocol: ssh\n"
            f"  oauth_token: {token}\n"
            f"  user: {login}\n"
        ))
        os.replace(temporary, directory / "hosts.yml")
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass
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

def generate_relay_pki(root):
    openssl='/usr/bin/openssl'
    if not Path(openssl).is_file() or not os.access(openssl,os.X_OK):
        raise RuntimeError("/usr/bin/openssl is required for Span transport")
    server=root/'server'; client=root/'client'; server.mkdir(parents=True,mode=0o700); client.mkdir(mode=0o700)
    ca_config=server/'ca.cnf'; server_ext=server/'server.ext'; client_ext=server/'client.ext'
    private_write(ca_config,"[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=v3\n[dn]\nCN=devc2 ephemeral Span CA\n[v3]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    private_write(server_ext,"[v3]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:host.docker.internal\n")
    private_write(client_ext,"[v3]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n")
    run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','30','-config',str(ca_config),'-keyout',str(server/'ca.key'),'-out',str(server/'ca.crt')],timeout=60)
    for name,common_name,extensions,serial in (
        ('server','host.docker.internal',server_ext,str(secrets.randbits(128) or 1)),
        ('client','devc2 Span gateway',client_ext,str(secrets.randbits(128) or 1)),
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

def compose_env(repo,public,relay_tls_dir=None,span_client_dir=None,spans=False):
    digest,project=identity(repo)
    world_env=public/'world.env'
    values={'DEVC2_PROJECT_NAME':project,'DEVC2_WORKSPACE':str(repo),'DEVC2_WORKSPACE_HASH':digest,'DEVC2_PUBLIC_DIR':str(public),'DEVC2_INSTALL_DIR':str(ROOT),'DEVC2_IMAGE':RUNTIME_IMAGE,'DEVC2_CREDENTIAL_IMAGE':SPAN_GATEWAY_IMAGE,'DEVC2_ASSET_DIGEST':ASSET_DIGEST}
    values['DEVC2_SPAN_RELAY_TLS_DIR']=str(relay_tls_dir or '/dev/null')
    values['DEVC2_SPAN_CLIENT_DIR']=str(span_client_dir or public)
    values['DEVC2_SPAN_VOLUME']=project+'-span-sockets-v2'
    values['DEVC2_WORLD_ENV_FILE']=str(world_env)
    if spans: values['COMPOSE_PROFILES']='spans'
    return docker_env(values)

def worktree_compose_override(temp,worktree):
    path=temp/"compose.worktree.yaml"
    def mount(source,target,read_only=False):
        result={"type":"bind","source":str(source),"target":str(target),"bind":{"create_host_path":False}}
        if read_only: result["read_only"]=True
        return result
    volumes=[
        mount(worktree.common,worktree.common_target),
        mount(worktree.common/"worktrees",worktree.common_target/"worktrees",True),
        mount(worktree.gitdir,worktree.gitdir_target),
        mount(worktree.pointer,Path("/workspace/.git"),True),
    ]
    volumes.append(mount(worktree.pointer,worktree.backlink_target,True))
    document={"services":{"devbox":{"volumes":volumes}}}
    path.write_text(json.dumps(document,separators=(",",":"))+"\n",encoding="utf-8")
    path.chmod(0o600)
    return path

def prepare(temp):
    public=temp/'public'; public.mkdir(0o700)
    shutil.copyfile(TACT_CONFIG,public/'tact-config.toml')
    (public / 'tact-config.toml').chmod(0o644)
    update_skill_projection(AGENT_SKILLS,public/'agent-skills')
    agent_home=public/'agent-home'; agent_home.mkdir(0o700)
    (agent_home/'skills').symlink_to('/run/devc2-public/agent-skills/current')
    agent_home.chmod(0o555)
    (public / 'world.env').write_text('',encoding='utf-8')
    (public / 'world.env').chmod(0o600)
    return public

def compose(repo,env,*args,capture=False,check=True):
    cmd=['docker','compose','--project-name',identity(repo)[1],'-f',str(ROOT/'compose.yaml'),*args]
    return run(cmd,env=env,capture=capture,check=check,timeout=1800)

def compose_run_command(repo,override=None):
    command=['docker','compose','--project-name',identity(repo)[1],'-f',str(ROOT/'compose.yaml')]
    if override: command.extend(['-f',str(override)])
    command.extend(['run','--rm','--no-deps'])
    return command

def remove_span_volume(name):
    if not isinstance(name,str) or not name.startswith("devc2-") or not name.endswith("-span-sockets-v2"):
        raise RuntimeError("invalid Span socket volume identity")
    run(["docker","volume","rm",name],check=False,timeout=30)
    if run(["docker","volume","inspect",name],check=False,timeout=15).returncode==0:
        raise RuntimeError("could not remove the previous Span socket volume")

def ensure_legacy_devcontainer_not_running(repo):
    result = run(["docker", "ps", "--filter", f"label=devcontainer.local_folder={repo}", "--format", "{{.ID}}"], timeout=15)
    if result.stdout.strip():
        raise RuntimeError("a legacy devcontainer is already running for this checkout; stop it before starting devc2")


def _start_unlocked(repo, runtime_doctor=False, span_names=()):
    require_docker()
    ensure_legacy_devcontainer_not_running(repo)
    ensure_tact_config()
    worktree=linked_worktree(repo)
    digest, _project = identity(repo)
    with checkout_lock(repo):
        with launch_directory() as temp:
            worktree_override=worktree_compose_override(temp,worktree) if worktree else None
            span_adapters=temp/'span-adapters'; span_worlds=temp/'span-worlds'
            granted_spans=span_runtime.prepare_span_grants(SPAN_CATALOG,span_names,repo,span_adapters,span_worlds,ROOT/'spans',ROOT/'launcher'/'command_adapter.py')
            empty=temp/'empty'; empty.mkdir(0o700)
            (empty/'world.env').write_text('',encoding='utf-8')
            (empty/'world.env').chmod(0o600)
            # A killed launcher may have left sidecars behind. Stop the old
            # project before projecting this launch's explicit grant set.
            cleanup_env=compose_env(repo,empty,span_client_dir=empty,spans=True)
            compose(repo,cleanup_env,'down','--remove-orphans',check=False)
            remove_span_volume(cleanup_env['DEVC2_SPAN_VOLUME'])
            public=prepare(temp)
            relay_server_dir=relay_client_dir=None
            spans=None
            try:
                if granted_spans:
                    relay_server_dir,relay_client_dir=generate_relay_pki(temp/'span-relay-pki')
                    spans=span_runtime.SpanRuntime(granted_spans,repo,f'{digest}-{secrets.token_hex(8)}',relay_server_dir)
                    spans.materialize_http_attachments(public)
                if granted_spans:
                    (public/'span-ready').write_text(secrets.token_hex(16))
                    (public/'span-ready').chmod(0o444)
                    (public/'spans.json').write_text(json.dumps([
                        {'name':item.name,'port':spans.ports[item.name]} for item in granted_spans
                    ],separators=(',',':')))
                    (public/'spans.json').chmod(0o444)
                env=compose_env(repo,public,relay_client_dir,span_adapters,bool(granted_spans))
            except Exception:
                if spans is not None: spans.stop()
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
                build_services=['devbox']
                if granted_spans: build_services.append('span-proxy')
                compose(repo,env,'build',*build_services)
                immutable_env=dict(env)
                immutable_env['DEVC2_IMAGE']=require_bound_image(RUNTIME_IMAGE)
                if granted_spans:
                    immutable_env['DEVC2_CREDENTIAL_IMAGE']=require_bound_image(SPAN_GATEWAY_IMAGE)
                env=immutable_env
                try:
                    if granted_spans: compose(repo,env,'up','--pull','never','--no-build','-d','span-proxy')
                except RuntimeError:
                    print("devc2: sanitized startup diagnostics:",file=sys.stderr)
                    if granted_spans: compose(repo,env,'logs','--no-color','--tail','100','span-proxy',capture=False,check=False)
                    raise
                command=compose_run_command(repo,worktree_override)
                command.extend(['--pull','never'])
                if runtime_doctor: command.extend(['--env','DEVC2_RUNTIME_DOCTOR=1'])
                command.append('devbox')
                result=subprocess.run(command,env=env).returncode
                if result:
                    print("devc2: sanitized Span diagnostics:",file=sys.stderr)
                    if granted_spans: compose(repo,env,'logs','--no-color','--tail','100','span-proxy',capture=False,check=False)
                return result
            except KeyboardInterrupt:
                return 130 if stopping else 1
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                if spans is not None: spans.stop()
                try:
                    down_args=['down','--remove-orphans']
                    if runtime_doctor: down_args.append('--volumes')
                    compose(repo,env,*down_args,check=False)
                finally:
                    try: remove_span_volume(env['DEVC2_SPAN_VOLUME'])
                    except RuntimeError as error: print(f"devc2: warning: {error}",file=sys.stderr)

def start(repo,runtime_doctor=False,span_names=()):
    with lifecycle_lock(False):
        return _start_unlocked(repo,runtime_doctor,span_names)

def _reset_unlocked(repo,yes):
    if not yes:
        try: answer=input(f"Permanently remove the Box home and checkout-scoped Docker resources for {repo}? [y/N] ")
        except EOFError: answer=''
        if answer.lower() not in {'y','yes'}: print('cancelled'); return 0
    digest,project=identity(repo)
    with checkout_lock(repo):
        dummy=STATE/'reset'; dummy.mkdir(parents=True,exist_ok=True)
        env=compose_env(repo,dummy)
        compose(repo,env,'down','--volumes','--remove-orphans')
        remaining=[]
        project_label=f"label=com.docker.compose.project={project}"
        for kind, command in (
            ('containers', ['docker','ps','--all','--quiet','--filter',project_label]),
            ('volumes', ['docker','volume','ls','--quiet','--filter',project_label]),
            ('networks', ['docker','network','ls','--quiet','--filter',project_label]),
        ):
            if run(command,timeout=30).stdout.strip(): remaining.append(kind)
        if remaining:
            raise RuntimeError(f"reset incomplete for {project}; remaining Docker resources: {', '.join(remaining)}")
        # Permanent lock inodes are intentionally retained to prevent reset/start races.
    print(f"reset {project}"); return 0

def reset(repo,yes):
    with lifecycle_lock(False): return _reset_unlocked(repo,yes)

def _service_container(project,service,workspace_hash=None,include_stopped=False):
    filters=[
        f"label=com.docker.compose.project={project}",
        f"label=com.docker.compose.service={service}",
        "label=dev.devbox.generation=2",
    ]
    if workspace_hash is not None:
        filters.append(f"label=dev.devbox.workspace-hash={workspace_hash}")
    command=["docker","ps"]
    if include_stopped: command.append("--all")
    command.append("--quiet")
    for item in filters: command.extend(("--filter",item))
    result=run(command,timeout=15)
    containers=result.stdout.split()
    if len(containers)!=1 or not re.fullmatch(r"[a-f0-9]{12,64}",containers[0]):
        detail="none" if not containers else str(len(containers))
        state="existing" if include_stopped else "running"
        raise RuntimeError(f"expected one {state} {service} container for {project}, found {detail}")
    return containers[0]

def _verify_world_diagnostics(raw,granted):
    try:
        span_entries=json.loads(raw)["links"]
        states={item["name"]:item["world_status"] for item in span_entries}
    except (ValueError,TypeError,KeyError) as error:
        raise RuntimeError("diagnostics Span returned invalid World status") from error
    failed=sorted(name for name in granted if states.get(name)!="running")
    if failed:
        raise RuntimeError(f"host Worlds are not running: {', '.join(failed)}")

def repair(repo):
    with lifecycle_lock(False), repair_lock(repo):
        require_docker()
        digest,project=identity(repo)
        devbox=_service_container(project,"devbox",digest)
        # A crashed Span gateway is a primary repair case, so include stopped containers.
        proxy=_service_container(project,"span-proxy",include_stopped=True)
        user=run(["docker","inspect","--format","{{.Config.User}}",devbox],timeout=15).stdout.strip()
        if user!="devbox":
            raise RuntimeError(f"running Box for {project} has unexpected runtime user")
        run([
            "docker","exec","--user","0",devbox,"rm","-f","/run/devc2/spans/.ready",
        ],timeout=10)
        run(["docker","restart",proxy],timeout=30)
        deadline=time.monotonic()+15
        ready=False
        while time.monotonic()<deadline:
            probe=run([
                "docker","exec",devbox,"cmp","-s",
                "/run/devc2-public/span-ready","/run/devc2/spans/.ready",
            ],check=False,timeout=10)
            if probe.returncode==0:
                ready=True
                break
            time.sleep(0.1)
        if not ready:
            raise RuntimeError(f"Span gateway for {project} did not become ready after restart")

        try:
            entries=json.loads(run([
                "docker","exec",devbox,"cat","/run/devc2-public/spans.json",
            ],timeout=10).stdout)
        except (ValueError,TypeError) as error:
            raise RuntimeError("running Box has an invalid grant manifest") from error
        if (not isinstance(entries,list) or any(
                not isinstance(item,dict) or set(item)!={"name","port"}
                or not isinstance(item["name"],str) or not span_runtime.valid_name(item["name"])
                or not isinstance(item["port"],int) or not 1<=item["port"]<=65535
                for item in entries)):
            raise RuntimeError("running Box has an invalid grant manifest")
        names=[item["name"] for item in entries]
        if len(names)!=len(set(names)):
            raise RuntimeError("running Box has an invalid grant manifest")
        granted=set(names)
        socket_root="/run/devc2/spans"
        for name in names:
            projected=run([
                "docker","exec",devbox,"/bin/sh","-c",f"test -S {socket_root}/{name}.sock",
            ],check=False,timeout=10)
            if projected.returncode!=0:
                raise RuntimeError(f"repaired Span gateway did not project {name!r}")
        if "diagnostics" in granted:
            status=run([
                "docker","exec",devbox,"/run/devc2/bin/diagnostics",
            ],timeout=15).stdout
            _verify_world_diagnostics(status,granted)
        ssh_socket=f"{socket_root}/ssh-agent.sock"
        has_ssh="ssh-agent" in granted
        if has_ssh:
            identities=run([
                "docker","exec","--env",f"SSH_AUTH_SOCK={ssh_socket}",devbox,
                "ssh-add","-L",
            ],timeout=15).stdout.splitlines()
            if len(identities)!=1 or not identities[0].startswith("ssh-"):
                raise RuntimeError("repaired SSH-agent Span did not expose exactly one identity")
            remote_script=f"/tmp/devc2-repair-signing-{secrets.token_hex(16)}"
            try:
                run(["docker","cp",str(ROOT/"box"/"configure-signing.sh"),f"{devbox}:{remote_script}"],timeout=30)
                run([
                    "docker","exec","--env",f"SSH_AUTH_SOCK={ssh_socket}",devbox,
                    "/bin/bash",remote_script,
                ],capture=False,timeout=60)
            finally:
                run(["docker","exec","--user","0",devbox,"rm","-f",remote_script],check=False,timeout=10)

        has_github="github" in granted
        if has_github:
            login=run([
                "docker","exec",devbox,"gh","api","user","--jq",".login",
            ],timeout=30).stdout.strip()
            if not login:
                raise RuntimeError("repaired GitHub Span returned no identity")

        print(f"restarted Span gateway and verified its endpoints for {project}")
        if has_ssh:
            print(f"✓ SSH-agent Span: one identity; signing configuration refreshed")
            print(f"note: existing shells may need: export SSH_AUTH_SOCK={ssh_socket}")
        if has_github: print(f"✓ GitHub Span: authenticated as {login}")
        return 0

def refresh_skills(repo):
    with lifecycle_lock(False), skills_refresh_lock(repo):
        require_docker()
        digest,project=identity(repo)
        devbox=_service_container(project,"devbox",digest)
        user=run(["docker","inspect","--format","{{.Config.User}}",devbox],timeout=15).stdout.strip()
        if user!="devbox":
            raise RuntimeError(f"running Box for {project} has unexpected runtime user")
        projection=_running_skill_projection(devbox)
        skills=update_skill_projection(AGENT_SKILLS,projection)
        if (_service_container(project,"devbox",digest)!=devbox
                or _running_skill_projection(devbox)!=projection):
            raise RuntimeError(f"running Box for {project} changed during skill refresh")
        expected=(projection/"current"/".devc2-generation").read_text(encoding="utf-8")
        visible=run([
            "docker","exec",devbox,"cat","/home/devbox/.agents/skills/.devc2-generation",
        ],check=False,timeout=15)
        if visible.returncode!=0 or visible.stdout!=expected:
            raise RuntimeError("refreshed agent skills are not visible inside the running Box")
        names=", ".join(skills) if skills else "none"
        print(f"refreshed {len(skills)} agent skill(s) for {project}: {names}")
        print("restart or reload agents in the Box when ready")
        return 0

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
    ssh_ready = False
    try:
        read_codex()
        codex_ready = True
    except RuntimeError:
        pass
    if (gh_home / "hosts.yml").exists():
        try:
            validate_github_token()
            github_ready = True
        except RuntimeError:
            try:
                import_host_github_auth()
                github_ready = True
            except RuntimeError:
                pass
    else:
        try:
            import_host_github_auth()
            github_ready = True
        except RuntimeError:
            pass
    try:
        select_key(False)
        ssh_ready = True
    except RuntimeError:
        pass

    if codex_ready and github_ready and ssh_ready:
        print("OpenAI, GitHub, and SSH host credentials are already configured.")
        return 0

    print("Preparing the pinned authentication image…")
    auth_image_id=ensure_auth_image()

    if not codex_ready:
        print("Signing in to ChatGPT…")
        run([
            "docker", "run", "--rm", "--pull=never", "-it", "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp/devc2-login", "--env", "CODEX_HOME=/home/devbox/.codex",
            "--volume", f"{codex_home}:/home/devbox/.codex",
            "--entrypoint", "codex", auth_image_id,
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
            "docker", "run", "--rm", "--pull=never", "-it", "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp", "--env", "GH_CONFIG_DIR=/run/devc2-gh",
            "--env", "GH_TOKEN=", "--env", "GITHUB_TOKEN=",
            "--volume", f"{gh_home}:/run/devc2-gh",
            "--entrypoint", "gh", auth_image_id,
            "auth", "login", "--hostname", "github.com",
            "--git-protocol", "ssh", "--web",
        ], capture=False, timeout=900)
        validate_github_token()
    else:
        print("GitHub authentication is already configured; skipping.")

    if not ssh_ready:
        print("Selecting the SSH identity exposed by the SSH-agent Span…")
        select_key(True)
    else:
        print("SSH identity is already configured; skipping.")

    print("Host credential setup saved outside all checkouts.")
    return 0

def doctor(runtime=False):
    checks=[]
    def check(label,fn):
        try: detail=fn(); checks.append((True,label,detail or 'ok'))
        except Exception as e: checks.append((False,label,str(e)))
    check('Docker Desktop',lambda: require_docker())
    check('OpenSSL',lambda: run(['/usr/bin/openssl','version'],timeout=10).stdout.strip())
    check('ChatGPT login',lambda: (read_codex() and 'access token has more than one hour remaining'))
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
        result=_start_unlocked(repo,runtime_doctor=True,span_names=("openai","github","ssh-agent","diagnostics"))
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
        description="Open a hardened, credential-isolated development environment.",
        epilog="commands: devc2 run <checkout> [--span NAME] | devc2 <checkout> | devc2 auth | devc2 doctor [--runtime] | devc2 repair <checkout> | devc2 skills refresh <checkout> | devc2 update | devc2 rollback | devc2 reset <checkout> [--yes]",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {installed_version(ROOT)}")
    return parser


def parse_cli(argv):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        usage_parser().print_help()
        raise SystemExit(0 if argv else 2)
    if argv[0] == "--version":
        print(f"devc2 {installed_version(ROOT)}")
        raise SystemExit(0)
    if argv[0] == "auth":
        if len(argv) != 1: raise RuntimeError("auth accepts no arguments")
        return "auth", None, False, ()
    if argv[0] in {"update","rollback"}:
        if len(argv)!=1: raise RuntimeError(f"{argv[0]} accepts no arguments")
        return argv[0],None,False,()
    if argv[0] == "doctor":
        parser=argparse.ArgumentParser(prog="devc2 doctor")
        parser.add_argument("--runtime",action="store_true",help="run disposable end-to-end Docker checks")
        args=parser.parse_args(argv[1:])
        return "doctor-runtime" if args.runtime else "doctor", None, False, ()
    if argv[0] == "reset":
        parser = argparse.ArgumentParser(
            prog="devc2 reset",
            description="permanently remove a checkout's persistent Box home and checkout-scoped Docker resources",
        )
        parser.add_argument("repo",metavar="CHECKOUT")
        parser.add_argument("--yes", action="store_true")
        args = parser.parse_args(argv[1:])
        return "reset", args.repo, args.yes, ()
    if argv[0] == "repair":
        parser = argparse.ArgumentParser(prog="devc2 repair")
        parser.add_argument("repo",metavar="CHECKOUT")
        args = parser.parse_args(argv[1:])
        return "repair", args.repo, False, ()
    if argv[:2] == ["skills","refresh"]:
        parser = argparse.ArgumentParser(prog="devc2 skills refresh")
        parser.add_argument("repo",metavar="CHECKOUT")
        args = parser.parse_args(argv[2:])
        return "skills-refresh", args.repo, False, ()
    if argv[0] == "skills":
        raise RuntimeError("usage: devc2 skills refresh <checkout>")
    explicit_run=argv[0]=="run" and len(argv)>1
    parser=argparse.ArgumentParser(prog="devc2 run" if explicit_run else "devc2")
    parser.add_argument("repo",metavar="CHECKOUT")
    parser.add_argument("--span",action="append",default=[],metavar="NAME")
    args=parser.parse_args(argv[1:] if explicit_run else argv)
    if len(set(args.span)) != len(args.span): raise RuntimeError("each Span may be granted only once")
    if len(args.span)>span_runtime.MAX_SPANS: raise RuntimeError(f"at most {span_runtime.MAX_SPANS} Spans may be granted")
    return "start",args.repo,False,tuple(args.span)


def main(argv=None):
    try:
        arguments=list(sys.argv[1:] if argv is None else argv)
        if arguments and arguments[0]=="_install-assets":
            parser=argparse.ArgumentParser(prog="devc2 _install-assets")
            parser.add_argument("--source",required=True); parser.add_argument("--bin-dir",required=True); parser.add_argument("--share-dir",required=True); parser.add_argument("--release",action="store_true")
            args=parser.parse_args(arguments[1:])
            return install_from_directory(Path(args.source),Path(args.bin_dir),Path(args.share_dir),args.release)
        command, raw_repo, yes, spans = parse_cli(arguments)
        if command in {"doctor","doctor-runtime"}:
            with lifecycle_lock(False): return doctor(runtime=command=="doctor-runtime")
        if command == "auth":
            with lifecycle_lock(False): return authenticate()
        if command == "update": return update_installation()
        if command == "rollback": return rollback_installation()
        repo = canonical_checkout(raw_repo)
        if command == "reset": return reset(repo, yes)
        if command == "repair": return repair(repo)
        if command == "skills-refresh": return refresh_skills(repo)
        return start(repo,span_names=spans)
    except RuntimeError as error:
        print(f"devc2: error: {error}", file=sys.stderr)
        return 1

if __name__=='__main__': raise SystemExit(main())
