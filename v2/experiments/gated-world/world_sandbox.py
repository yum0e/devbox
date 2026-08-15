#!/usr/bin/env python3
"""Remove ambient authority from a Linux process after its Links are open."""

import ctypes
import errno
import os


LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_ACCESS_FS = sum(1 << bit for bit in range(16))
LANDLOCK_ACCESS_NET = (1 << 0) | (1 << 1)  # bind/connect TCP
PR_SET_NO_NEW_PRIVS = 38
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM

# These operations are unnecessary once the Gate has opened its listener and
# Links. Landlock separately denies every new filesystem access and TCP action.
DENIED_SYSCALLS = (
    "add_key", "bpf", "bind", "clone", "clone3", "connect", "execve",
    "execveat", "fork", "keyctl", "kill", "mount", "open_by_handle_at",
    "perf_event_open", "pidfd_getfd", "pidfd_open", "pidfd_send_signal",
    "process_vm_readv", "process_vm_writev", "ptrace", "request_key",
    "setns", "socket", "tgkill", "tkill", "umount2", "unshare", "vfork",
)


class Ruleset(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


def landlock_abi() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(444, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    return int(result)


def _landlock() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = landlock_abi()
    if abi < 6:
        raise RuntimeError(f"Landlock ABI 6 is required, found {abi}")
    attributes = Ruleset(LANDLOCK_ACCESS_FS, LANDLOCK_ACCESS_NET, 0)
    descriptor = libc.syscall(444, ctypes.byref(attributes), ctypes.sizeof(attributes), 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
        if libc.syscall(446, descriptor, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(descriptor)


def _seccomp() -> None:
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]

    context = library.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        for name in DENIED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                continue
            result = library.seccomp_rule_add(context, SCMP_ACT_ERRNO, number, 0)
            if result != 0:
                raise OSError(-result, f"seccomp rule for {name}")
        result = library.seccomp_load(context)
        if result != 0:
            raise OSError(-result, "seccomp_load")
    finally:
        library.seccomp_release(context)


def restrict_world() -> None:
    """Keep existing descriptors, but remove filesystem, network, and exec power."""
    _seccomp()
    _landlock()
    os.environ.clear()
