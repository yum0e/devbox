#!/usr/bin/env bash
set -euo pipefail

# Download first, then execute this file; do not pipe it directly into a shell.
base="https://github.com/yum0e/devbox/releases"
if [[ -n "${DEVC2_RELEASE_TAG:-}" ]]; then release_url="$base/download/$DEVC2_RELEASE_TAG"; else release_url="$base/latest/download"; fi
command -v python3 >/dev/null 2>&1 || { echo "install-devc2: python3 is required" >&2; exit 1; }
[[ -x /usr/bin/openssl ]] || { echo "install-devc2: /usr/bin/openssl is required" >&2; exit 1; }
temporary="$(mktemp -d "${TMPDIR:-/tmp}/devc2-install.XXXXXX")"
cleanup() { rm -rf -- "$temporary"; }
trap cleanup EXIT HUP INT TERM
chmod 0700 "$temporary"
python3 - "$release_url" "$temporary" <<'PY'
import gzip,hashlib,os,re,sys,tarfile,unicodedata,urllib.parse,urllib.request
from pathlib import Path,PurePosixPath
base=sys.argv[1]; root=Path(sys.argv[2]); hosts={'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
def trusted(url):
    parsed=urllib.parse.urlparse(url)
    return parsed.scheme=='https' and parsed.hostname in hosts and parsed.username is None and parsed.password is None
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        if not trusted(newurl): raise SystemExit('install-devc2: untrusted release redirect')
        return super().redirect_request(request,fp,code,msg,headers,newurl)
opener=urllib.request.build_opener(Redirect())
def download(name,limit):
    url=f'{base}/{name}'
    if not trusted(url): raise SystemExit('install-devc2: untrusted release URL')
    request=urllib.request.Request(url,headers={'User-Agent':'devc2-installer'})
    target=root/name; total=0; digest=hashlib.sha256()
    with opener.open(request,timeout=30) as response, target.open('xb') as output:
        if not trusted(response.geturl()): raise SystemExit('install-devc2: untrusted final release URL')
        while True:
            chunk=response.read(min(1024*1024,limit-total+1))
            if not chunk: break
            total+=len(chunk)
            if total>limit: raise SystemExit('install-devc2: release download exceeds its size limit')
            output.write(chunk); digest.update(chunk)
    return target,digest.hexdigest()
archive,actual=download('devc2.tar.gz',32*1024*1024)
checksum,_=download('devc2.tar.gz.sha256',4096)
match=re.fullmatch(r'([0-9a-f]{64})  devc2\.tar\.gz\n',checksum.read_text('ascii'))
if not match or not __import__('hmac').compare_digest(actual,match.group(1)): raise SystemExit('install-devc2: release checksum verification failed')
# Bound decompression before tarfile can materialize PAX/GNU extension headers.
tar_path=root/'devc2.tar'; expanded=0
with gzip.open(archive,'rb') as source,tar_path.open('xb') as output:
    while True:
        chunk=source.read(1024*1024)
        if not chunk: break
        expanded+=len(chunk)
        if expanded>40*1024*1024: raise SystemExit('install-devc2: release archive expands beyond its size limit')
        output.write(chunk)
destination=root/'extracted'; destination.mkdir(0o700); entries=[]; names=set(); total=0; limit=32*1024*1024
with tarfile.open(tar_path,'r:') as package:
    while True:
        member=package.next()
        if member is None: break
        raw=member.name.rstrip('/') if member.isdir() else member.name; parts=raw.split('/'); normalized=unicodedata.normalize('NFC',raw).casefold()
        if len(entries)>=256 or not raw or '\\' in raw or any(x in {'','.','..'} for x in parts) or PurePosixPath(raw).is_absolute() or parts[0]!='devc2' or normalized in names: raise SystemExit('install-devc2: unsafe release archive')
        if not (member.isdir() or member.isfile()) or any(k.startswith('GNU.sparse') for k in member.pax_headers): raise SystemExit('install-devc2: release contains links or special files')
        names.add(normalized); total+=member.size
        if member.size>limit or total>limit: raise SystemExit('install-devc2: release is too large')
        entries.append((member.name,member.size,member.isdir()))
with tarfile.open(tar_path,'r:') as package:
    for name,size,isdir in entries:
        member=package.next()
        if member is None or (member.name,member.size,member.isdir())!=(name,size,isdir): raise SystemExit('install-devc2: archive changed during verification')
        raw=name.rstrip('/') if isdir else name; target=destination.joinpath(*raw.split('/'))
        if isdir: target.mkdir(parents=True,exist_ok=True)
        else:
            target.parent.mkdir(parents=True,exist_ok=True); source=package.extractfile(member)
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600); written=0
            with os.fdopen(fd,'wb') as output:
                while True:
                    chunk=source.read(min(1024*1024,size-written+1))
                    if not chunk: break
                    written+=len(chunk)
                    if written>size: raise SystemExit('install-devc2: oversized archive member')
                    output.write(chunk)
            if written!=size: raise SystemExit('install-devc2: truncated archive member')
PY
DEVC2_INSTALL_RELEASE=1 "$temporary/extracted/devc2/install.sh"
