from __future__ import annotations
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).parents[1]
SPEC=importlib.util.spec_from_file_location("devc2_update_module",ROOT/"launcher"/"devc2.py")
D=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(D)

class UpdateTests(unittest.TestCase):
    def runtime(self,root):
        D.STATE=root/"state"; D.INSTALL_LOCK=D.STATE/"install.lock"; D.INSTALL_STATE=D.STATE/"installation.json"

    def test_versioned_install_and_rollback_preserve_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root)
            config=root/"config"; config.mkdir(); sentinel=config/"settings"; sentinel.write_bytes(b"unchanged\x00config")
            repo_state=D.STATE/"existing-repo"; repo_state.mkdir(parents=True); state_sentinel=repo_state/"session"; state_sentinel.write_bytes(b"unchanged state")
            source1=root/"source1"; source2=root/"source2"
            shutil.copytree(ROOT,source1,ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT,source2,ignore=shutil.ignore_patterns("__pycache__"))
            (source2/"README.md").write_text((source2/"README.md").read_text()+"\nsecond build\n")
            bin_dir=root/"bin"; share=root/"share"
            with mock.patch.dict(os.environ,{"DEVC2_BIN_DIR":str(bin_dir),"DEVC2_DATA_DIR":str(share)},clear=False), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(D.install_from_directory(source1,bin_dir,share),0)
                first=(share/"current").resolve()
                self.assertEqual(D.install_from_directory(source2,bin_dir,share),0)
                second=(share/"current").resolve()
                self.assertNotEqual(first,second)
                self.assertEqual((share/"previous").resolve(),first)
                environment={**os.environ,"DEVC2_DATA_DIR":str(share),"XDG_STATE_HOME":str(root/"dispatcher-state")}
                completed=subprocess.run([sys.executable,str(share/"bootstrap"/"dispatcher.py"),"rollback"],env=environment,text=True,capture_output=True)
                self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual((share/"current").resolve(),first)
            self.assertEqual((share/"previous").resolve(),second)
            self.assertEqual((share/"manager").resolve(),second)
            self.assertEqual(sentinel.read_bytes(),b"unchanged\x00config")
            self.assertEqual(state_sentinel.read_bytes(),b"unchanged state")
            self.assertFalse((root/".zshrc").exists())
            self.assertIn("bootstrap/dispatcher.py",(bin_dir/"devc2").read_text())

    def test_install_refuses_while_shared_session_lock_is_held(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root); D.STATE.mkdir()
            with D.INSTALL_LOCK.open("a+") as lock:
                fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError,"session is active"):
                    D.install_from_directory(ROOT,root/"bin",root/"share")

    def test_legacy_repo_lock_makes_reset_refuse(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root); repo=root/"repo"; repo.mkdir(); digest=D.identity(repo)[0]
            legacy=D.STATE/digest/"launch.lock"; legacy.parent.mkdir(parents=True)
            with legacy.open("a+") as lock, mock.patch.object(D,"compose") as compose:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError,"already running"): D._reset_unlocked(repo,True)
            compose.assert_not_called()

    def test_first_migration_failure_leaves_legacy_launcher_usable(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root); bin_dir=root/"bin"; share=root/"share"; bin_dir.mkdir(); share.mkdir()
            legacy=bin_dir/"devc2"; original='#!/usr/bin/env bash\nexport DEVC2_SHARE_DIR='+str(share)+'\nexec python3 "$DEVC2_SHARE_DIR/launcher/devc2.py" "$@"\n'; legacy.write_text(original)
            real_replace=D.replace_symlink
            def fail_current(link,target):
                if link.name=="current": raise OSError("injected activation failure")
                return real_replace(link,target)
            with mock.patch.object(D,"replace_symlink",side_effect=fail_current), self.assertRaisesRegex(OSError,"injected"):
                D.install_from_directory(ROOT,bin_dir,share)
            self.assertEqual(legacy.read_text(),original)

    def test_repo_lock_makes_reset_refuse_a_live_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root); repo=root/"repo"; repo.mkdir()
            with D.repository_lock(repo), mock.patch.object(D,"compose") as compose:
                with self.assertRaisesRegex(RuntimeError,"already running"):
                    D._reset_unlocked(repo,True)
            compose.assert_not_called()

    def malicious_archive(self,path,name,type_):
        with tarfile.open(path,"w:gz") as package:
            info=tarfile.TarInfo(name); info.type=type_; info.size=0
            if type_==tarfile.SYMTYPE: info.linkname="../../escape"
            package.addfile(info)

    def test_archive_rejects_traversal_and_links_before_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for index,(name,type_) in enumerate((("../escape",tarfile.REGTYPE),("devc2/link",tarfile.SYMTYPE))):
                archive=root/f"bad{index}.tgz"; self.malicious_archive(archive,name,type_); output=root/f"out{index}"
                with self.assertRaisesRegex(RuntimeError,"unsafe|links"):
                    D.extract_release(archive,output)
                self.assertFalse((root/"escape").exists())

    def test_archive_decompression_is_bounded_before_tar_parsing(self):
        import gzip
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); archive=root/"bomb.tgz"
            with gzip.open(archive,"wb",compresslevel=9) as output:
                chunk=b"0"*(1024*1024)
                for _ in range(41): output.write(chunk)
            with self.assertRaisesRegex(RuntimeError,"expands beyond"): D.extract_release(archive,root/"out")

    def test_release_metadata_requires_one_asset_and_github_digest(self):
        base={"tag_name":"devc2-v0.2.1","assets":[]}
        with mock.patch.object(D,"read_https",return_value=json.dumps(base).encode()):
            with self.assertRaisesRegex(RuntimeError,"exactly one"): D.latest_release()
        asset={"name":"devc2.tar.gz","browser_download_url":"https://github.com/yum0e/devbox/releases/download/devc2-v0.2.1/devc2.tar.gz","digest":"sha256:"+"a"*64}
        base["assets"]=[asset,dict(asset)]
        with mock.patch.object(D,"read_https",return_value=json.dumps(base).encode()):
            with self.assertRaisesRegex(RuntimeError,"exactly one"): D.latest_release()
        base["assets"]=[{**asset,"digest":"sha256:not-hex"}]
        with mock.patch.object(D,"read_https",return_value=json.dumps(base).encode()):
            with self.assertRaisesRegex(RuntimeError,"digest"): D.latest_release()

    def test_update_verifies_download_digest_before_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root); source=root/"source"; shutil.copytree(ROOT,source,ignore=shutil.ignore_patterns("__pycache__"))
            launcher=source/"launcher"/"devc2.py"; launcher.write_text(launcher.read_text().replace('VERSION="0.2.0"','VERSION="0.2.1"',1))
            payload=b"verified release archive"; digest=D.hashlib.sha256(payload).hexdigest()
            with mock.patch.object(D,"latest_release",return_value=("0.2.1","https://github.com/yum0e/devbox/releases/download/devc2-v0.2.1/devc2.tar.gz",digest)), mock.patch.object(D,"read_https",return_value=payload), mock.patch.object(D,"extract_release",return_value=source), mock.patch.object(D,"_activate_assets",return_value=0) as activate:
                self.assertEqual(D.update_installation(),0)
            activate.assert_called_once()
            with mock.patch.object(D,"latest_release",return_value=("0.2.1","https://github.com/yum0e/devbox/releases/download/devc2-v0.2.1/devc2.tar.gz","0"*64)), mock.patch.object(D,"read_https",return_value=payload), mock.patch.object(D,"_activate_assets") as activate:
                with self.assertRaisesRegex(RuntimeError,"digest does not match"): D.update_installation()
            activate.assert_not_called()

    def test_update_uses_invoked_wrapper_paths_not_global_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.runtime(root); D.STATE.mkdir(); source=root/"source"; shutil.copytree(ROOT,source,ignore=shutil.ignore_patterns("__pycache__"))
            launcher=source/"launcher"/"devc2.py"; launcher.write_text(launcher.read_text().replace('VERSION="0.2.0"','VERSION="0.2.1"',1))
            D.atomic_write(D.INSTALL_STATE,json.dumps({"schema":1,"bin_dir":str(root/"wrong-bin"),"share_dir":str(root/"wrong-share")})+"\n")
            payload=b"archive"; digest=D.hashlib.sha256(payload).hexdigest(); bin_a=root/"bin-a"; share_a=root/"share-a"
            with mock.patch.dict(os.environ,{"DEVC2_BIN_DIR":str(bin_a),"DEVC2_DATA_DIR":str(share_a)},clear=False), mock.patch.object(D,"latest_release",return_value=("0.2.1","https://github.com/yum0e/devbox/releases/download/devc2-v0.2.1/devc2.tar.gz",digest)), mock.patch.object(D,"read_https",return_value=payload), mock.patch.object(D,"extract_release",return_value=source), mock.patch.object(D,"_activate_assets",return_value=0) as activate:
                self.assertEqual(D.update_installation(),0)
            self.assertEqual(activate.call_args.args[1:3],(bin_a,share_a))

    def test_redirect_policy_rejects_http_and_non_github_hosts(self):
        handler=D.TrustedRedirectHandler()
        request=D.urllib.request.Request("https://github.com/yum0e/devbox/releases/latest")
        for url in ("http://github.com/file","https://example.com/file","https://evilgithubusercontent.com/file"):
            with self.assertRaises(RuntimeError): handler.redirect_request(request,None,302,"",{},url)

    def test_dispatcher_holds_shared_lock_across_exec(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); data=root/"data"; runtime=data/"versions"/"test"; launcher=runtime/"launcher"; launcher.mkdir(parents=True); data.mkdir(exist_ok=True)
            ready=root/"ready"
            (launcher/"devc2.py").write_text(f"import pathlib,time\npathlib.Path({str(ready)!r}).write_text('ready')\ntime.sleep(30)\n")
            os.symlink(runtime,data/"current")
            state_home=root/"state-home"
            env={**os.environ,"DEVC2_DATA_DIR":str(data),"XDG_STATE_HOME":str(state_home)}
            process=subprocess.Popen([sys.executable,str(ROOT/"launcher"/"dispatcher.py"),"dummy"],env=env)
            try:
                deadline=time.time()+5
                while not ready.exists() and time.time()<deadline: time.sleep(.02)
                self.assertTrue(ready.exists())
                lock_path=state_home/"devc2"/"install.lock"
                with lock_path.open("a+") as lock, self.assertRaises(BlockingIOError):
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                process.terminate(); process.wait(timeout=5)

    def test_release_packager_rejects_top_level_directory_symlink(self):
        package_spec=importlib.util.spec_from_file_location("devc2_package",ROOT/"package-release.py"); package=importlib.util.module_from_spec(package_spec); package_spec.loader.exec_module(package)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); external=root/"external"; external.mkdir(); (external/"secret").write_text("must not package")
            os.symlink(external,root/"devbox"); package.ROOT=root
            with self.assertRaisesRegex(RuntimeError,"symlink"): package.files()

    def test_installer_never_uses_sudo_or_shell_startup_files(self):
        text=(ROOT/"install.sh").read_text()+(ROOT/"launcher"/"devc2.py").read_text()
        self.assertNotIn("sudo",text)
        for name in (".zshrc",".bashrc",".profile",".zprofile"): self.assertNotIn(name,text)
        self.assertNotIn("--force",text)
        workflow=(ROOT.parent/".github"/"workflows"/"devc2-release.yml").read_text()
        self.assertIn("github.ref_protected",workflow)
        self.assertIn("merge-base --is-ancestor",workflow)
        self.assertIn("environment: devc2-release",workflow)

if __name__=="__main__": unittest.main()
