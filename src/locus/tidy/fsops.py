"""The one place a file is renamed: exclusive, so an existing destination is an error, never a replacement."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys

RENAME_EXCL_MACOS = 0x00000004  # sys/stdio.h
RENAME_NOREPLACE_LINUX = 1  # linux/fs.h
AT_FDCWD = -100


def _libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    except OSError:
        return None


def rename_exclusive(src: str, dst: str) -> str:
    """Move *src* to *dst*; raise FileExistsError if *dst* exists. Returns the method used.

    macOS: renamex_np(RENAME_EXCL). Linux: renameat2(RENAME_NOREPLACE). Otherwise a hard link followed by
    an unlink, which also fails if *dst* exists (the source briefly has two names). All three keep the inode,
    so extended attributes such as the quarantine flag and Finder tags stay with the file on one volume."""
    libc = _libc()
    s, d = os.fsencode(src), os.fsencode(dst)
    if sys.platform == "darwin" and libc is not None and hasattr(libc, "renamex_np"):
        libc.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        if libc.renamex_np(s, d, RENAME_EXCL_MACOS) == 0:
            return "renamex_np"
        err = ctypes.get_errno()
        if err not in (errno.ENOTSUP, errno.EINVAL):
            raise _oserror(err, dst)
    elif sys.platform.startswith("linux") and libc is not None and hasattr(libc, "renameat2"):
        libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        if libc.renameat2(AT_FDCWD, s, AT_FDCWD, d, RENAME_NOREPLACE_LINUX) == 0:
            return "renameat2"
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL):
            raise _oserror(err, dst)
    os.link(src, dst, follow_symlinks=False)  # FileExistsError if dst exists
    try:
        os.unlink(src)
    except OSError:
        os.unlink(dst)  # keep exactly one name
        raise
    return "link+unlink"


def _oserror(err: int, dst: str) -> OSError:
    if err == errno.EEXIST:
        return FileExistsError(err, os.strerror(err), dst)
    return OSError(err, os.strerror(err), dst)
