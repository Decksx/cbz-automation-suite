"""Operator-only access control for the console's state directory and secret files.

The default state directory sits under ``G:\\ComicAutomation``, which grants
*Authenticated Users* Modify. A directory created there inherits that, so any
other account on the machine could read ``session.json`` (the launcher secret)
or edit ``console.toml`` and the approval ledger. The state directory is
therefore made private when it is created and checked for privacy on every
start: a protected DACL (no inheritance) whose allow entries name only the
operator, SYSTEM and the Administrators group.

Accounts are named by SID, never by ``USERDOMAIN\\USERNAME`` from the
environment, which is wrong for Microsoft and Azure AD accounts.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
# SDDL writes some well-known SIDs as two-letter aliases.
_ALIASES = {"SY": SYSTEM_SID, "BA": ADMINISTRATORS_SID}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AclError(RuntimeError):
    """The access control list could not be applied or read."""


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _OWNER = 0x1
    _DACL = 0x4
    _SE_FILE_OBJECT = 1
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    _kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    _advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                              ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.POINTER(ctypes.c_void_p)]
    _advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]

    def operator_sid() -> str:
        """The SID of the account running this process, from its own token."""
        token = wintypes.HANDLE()
        if not _advapi.OpenProcessToken(_kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise AclError(f"OpenProcessToken failed: WinError {ctypes.get_last_error()}")
        try:
            size = wintypes.DWORD()
            _advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _advapi.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
                raise AclError(f"GetTokenInformation failed: WinError {ctypes.get_last_error()}")
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            text = wintypes.LPWSTR()
            if not _advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
                raise AclError(f"ConvertSidToStringSid failed: WinError {ctypes.get_last_error()}")
            try:
                return text.value
            finally:
                _kernel.LocalFree(text)
        finally:
            _kernel.CloseHandle(token)

    def sddl(path: Path) -> str:
        """Owner and DACL of ``path`` in SDDL form."""
        sd = ctypes.c_void_p()
        err = _advapi.GetNamedSecurityInfoW(str(path), _SE_FILE_OBJECT, _OWNER | _DACL, None, None, None, None,
                                            ctypes.byref(sd))
        if err:
            raise AclError(f"Cannot read the security of {path}: WinError {err}")
        try:
            text = wintypes.LPWSTR()
            if not _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, _OWNER | _DACL,
                                                                                 ctypes.byref(text), None):
                raise AclError(f"Cannot format the security of {path}: WinError {ctypes.get_last_error()}")
            try:
                return text.value
            finally:
                _kernel.LocalFree(text)
        finally:
            _kernel.LocalFree(sd)

    def _icacls(*args: str) -> None:
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
        result = subprocess.run([str(exe), *args], capture_output=True, text=True, creationflags=_NO_WINDOW)
        if result.returncode != 0:
            raise AclError((result.stdout + result.stderr).strip())

    def make_private(path: Path, *, directory: bool) -> None:
        """Replace inherited access with operator, SYSTEM and Administrators only."""
        inherit = "(OI)(CI)" if directory else ""
        grants = [f"*{sid}:{inherit}F" for sid in (operator_sid(), SYSTEM_SID, ADMINISTRATORS_SID)]
        _icacls(str(path), "/inheritance:r", "/grant:r", *grants)

    def privacy_problems(path: Path) -> list[str]:
        """Why ``path`` is not private to the operator; empty when it is."""
        text = sddl(path)
        me = operator_sid()
        allowed = {me, SYSTEM_SID, ADMINISTRATORS_SID}
        problems = []
        owner = re.search(r"O:([^:]+?)(?=[GDS]:|$)", text)
        owner_sid = _ALIASES.get(owner.group(1), owner.group(1)) if owner else None
        if owner_sid not in allowed:
            problems.append(f"owned by {owner_sid}, not by the operator")
        dacl = re.search(r"D:([A-Z]*)(\(.*)?$", text)
        if not dacl:
            return problems + ["has no access list (everyone has full access)"]
        if "P" not in dacl.group(1):
            problems.append("inherits access from its parent folder")
        for ace in re.findall(r"\(([^)]*)\)", dacl.group(2) or ""):
            fields = ace.split(";")
            if fields[0] in ("A", "OA"):  # allow entries; deny entries only take access away
                sid = _ALIASES.get(fields[5], fields[5])
                if sid not in allowed:
                    problems.append(f"grants access to {sid}")
        return problems

else:  # pragma: no cover - the console is Windows-first; POSIX keeps tests importable

    def operator_sid() -> str:
        return str(os.getuid())

    def make_private(path: Path, *, directory: bool) -> None:
        os.chmod(path, 0o700 if directory else 0o600)

    def privacy_problems(path: Path) -> list[str]:
        st = os.stat(path)
        problems = []
        if st.st_uid != os.getuid():
            problems.append("owned by another user")
        if st.st_mode & 0o077:
            problems.append("readable or writable by other users")
        return problems
