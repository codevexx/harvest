"""Installing apps on this PC for the dashboard's "Apps" dialog.

The owner picks an app in the dashboard (or approves one Claude asked for through the
MCP); the server sends the request with the next check-in and this module runs it with
the system's package manager:

    Windows  winget, always --scope user: only per-user installers, never admin
    macOS    Homebrew (if installed), as this user
    Linux    Homebrew (if installed), else Flatpak --user

Never with admin rights: an app that only has a system-wide installer fails with a
clear message instead of asking for elevation. If Harvest itself runs with an admin
token (Windows gives one to a startup task of an admin account), installs run with a
standard-user copy of it instead, like UAC does for normal apps (_run_limited). Package
ids are checked here too, and every program runs from an argument list (no shell), so a
request can't smuggle in extra commands.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WINDOWS = sys.platform.startswith("win")
MAC = sys.platform == "darwin"
INSTALL_TIMEOUT = 40 * 60
STATE_FILE = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".harvest") / "Harvest" / "apps.json"
              if WINDOWS else Path.home() / ".harvest" / "apps.json")

ID_OK = {
    "winget": re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$"),
    "brew": re.compile(r"^[a-z0-9][a-z0-9@._+-]{0,99}(/[a-z0-9][a-z0-9@._+-]{0,99}){0,2}$"),
    "flatpak": re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$"),
}
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW


def _elevated() -> bool:
    try:
        if WINDOWS:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def _winget() -> str | None:
    found = shutil.which("winget")
    if found:
        return found
    # The App Installer's execution alias can be missing in background sessions: use the package directly.
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe"
    if local.exists():
        return str(local)
    try:
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        dirs = sorted(root.glob("Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe"), reverse=True)
        for d in dirs:
            if (d / "winget.exe").exists():
                return str(d / "winget.exe")
    except OSError:
        pass
    return None


def _brew() -> str | None:
    for p in (shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew",
              "/home/linuxbrew/.linuxbrew/bin/brew", str(Path.home() / ".linuxbrew" / "bin" / "brew")):
        if p and os.path.exists(p):
            return p
    return None


def managers() -> list[str]:
    """The package managers this PC can install with (reported in the machine's specs)."""
    if WINDOWS:
        return ["winget"] if _winget() else []
    if _elevated():
        return []  # running as root: no standard-user fallback here, and installs never use admin rights
    out = ["brew"] if _brew() else []
    if not MAC and shutil.which("flatpak"):
        out.append("flatpak")
    return out


_desktop_ok = False


def _grant_desktop(tok) -> None:
    """Background (session 0) Harvest runs in a hidden window station that only its
    owner, the Administrators group, may use. A process with Administrators switched
    off can't start there (error 0xC0000142), so also let this user's own account in.
    Once per run; nobody sees this station, and it is the same user."""
    global _desktop_ok
    if _desktop_ok:
        return
    import ctypes
    from ctypes import wintypes as W
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class TRUSTEE(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]

    class EA(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", W.DWORD), ("grfAccessMode", ctypes.c_int), ("grfInheritance", W.DWORD),
                    ("Trustee", TRUSTEE)]

    u32.GetProcessWindowStation.restype = W.HANDLE
    u32.GetThreadDesktop.restype = W.HANDLE
    u32.GetThreadDesktop.argtypes = [W.DWORD]
    u32.GetUserObjectInformationW.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    u32.OpenWindowStationW.restype = W.HANDLE
    u32.OpenWindowStationW.argtypes = [W.LPCWSTR, W.BOOL, W.DWORD]
    u32.OpenDesktopW.restype = W.HANDLE
    u32.OpenDesktopW.argtypes = [W.LPCWSTR, W.DWORD, W.BOOL, W.DWORD]
    u32.CloseWindowStation.argtypes = [W.HANDLE]
    u32.CloseDesktop.argtypes = [W.HANDLE]
    adv.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    adv.GetSecurityInfo.argtypes = [W.HANDLE, ctypes.c_int, W.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.SetEntriesInAclW.argtypes = [W.ULONG, ctypes.POINTER(EA), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.SetSecurityInfo.argtypes = [W.HANDLE, ctypes.c_int, W.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    def name(h):
        buf, need = ctypes.create_unicode_buffer(256), W.DWORD()
        return buf.value if u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(need)) else None

    ws_name = name(u32.GetProcessWindowStation())
    dk_name = name(u32.GetThreadDesktop(k32.GetCurrentThreadId())) or "Default"
    if not ws_name or ws_name.lower() == "winsta0":
        _desktop_ok = True  # the signed-in desktop already lets the user in
        return
    need = W.DWORD()
    adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(need))  # TokenUser
    buf = ctypes.create_string_buffer(need.value)
    if not adv.GetTokenInformation(tok, 1, buf, need, ctypes.byref(need)):
        raise OSError(f"could not read this user's account (error {ctypes.get_last_error()})")
    user_sid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
    READ_CONTROL_WRITE_DAC = 0x20000 | 0x40000
    for kind, h in (("station", u32.OpenWindowStationW(ws_name, False, READ_CONTROL_WRITE_DAC)),
                    ("desktop", u32.OpenDesktopW(dk_name, 0, False, READ_CONTROL_WRITE_DAC))):
        if not h:
            raise OSError(f"could not open the background {kind} (error {ctypes.get_last_error()})")
        try:
            old, sd, new = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            err = adv.GetSecurityInfo(h, 7, 4, None, None, ctypes.byref(old), None, ctypes.byref(sd))  # SE_WINDOW_OBJECT, DACL
            if err:
                raise OSError(f"could not read the background {kind}'s permissions (error {err})")
            ea = EA(0x10000000, 1, 0, TRUSTEE(None, 0, 0, 1, user_sid))  # GENERIC_ALL, GRANT_ACCESS, TRUSTEE_IS_SID/USER
            err = adv.SetEntriesInAclW(1, ctypes.byref(ea), old, ctypes.byref(new))
            if not err:
                err = adv.SetSecurityInfo(h, 7, 4, None, None, new, None)
            k32.LocalFree(sd)
            if new.value:
                k32.LocalFree(new)
            if err:
                raise OSError(f"could not let this user use the background {kind} (error {err})")
        finally:
            (u32.CloseWindowStation if kind == "station" else u32.CloseDesktop)(h)
    _desktop_ok = True


def _run_limited(cmd: list[str], timeout: int, env: dict) -> tuple[int, str]:
    """Windows: run cmd with a standard-user version of Harvest's own admin token
    (Administrators switched off, no privileges, medium integrity), output to a temp file."""
    import ctypes
    from ctypes import wintypes as W
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class SA(ctypes.Structure):
        _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", W.BOOL)]

    class SID_ATTR(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", W.DWORD)]

    class SI(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                    ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                    ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                    ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]

    class PI(ctypes.Structure):
        _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]

    adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
    adv.ConvertStringSidToSidW.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.CreateRestrictedToken.argtypes = [W.HANDLE, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, ctypes.c_void_p,
                                          W.DWORD, ctypes.c_void_p, ctypes.POINTER(W.HANDLE)]
    adv.GetLengthSid.argtypes = [ctypes.c_void_p]
    adv.SetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    adv.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.BOOL)]
    adv.CreateProcessAsUserW.argtypes = [W.HANDLE, W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL, W.DWORD,
                                         ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(SI), ctypes.POINTER(PI)]
    k32.GetCurrentProcess.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.POINTER(SA), W.DWORD, W.DWORD, W.HANDLE]
    k32.CreateFileW.restype = W.HANDLE
    k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    k32.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    k32.CloseHandle.argtypes = [W.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    def fail(what):
        raise OSError(f"could not drop admin rights ({what}, error {ctypes.get_last_error()})")

    handles, sids = [], []
    tok, lim = W.HANDLE(), W.HANDLE()
    try:
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0xF01FF, ctypes.byref(tok)):  # TOKEN_ALL_ACCESS
            fail("OpenProcessToken")
        handles.append(tok)
        _grant_desktop(tok)
        admins, local_admin, medium = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        for sid_str, ref in (("S-1-5-32-544", admins), ("S-1-5-114", local_admin), ("S-1-16-8192", medium)):
            if not adv.ConvertStringSidToSidW(sid_str, ctypes.byref(ref)):
                fail("ConvertStringSidToSid")
            sids.append(ref)
        # Administrators (and "local account and member of Administrators") made deny-only, as UAC does
        disable = (SID_ATTR * 2)(SID_ATTR(admins, 0), SID_ATTR(local_admin, 0))
        # DISABLE_MAX_PRIVILEGE | LUA_TOKEN
        if not adv.CreateRestrictedToken(tok, 0x1 | 0x4, 2, disable, 0, None, 0, None, ctypes.byref(lim)):
            fail("CreateRestrictedToken")
        handles.append(lim)

        class TML(ctypes.Structure):
            _fields_ = [("Label", SID_ATTR)]
        label = TML(SID_ATTR(medium, 0x20))  # SE_GROUP_INTEGRITY
        if not adv.SetTokenInformation(lim, 25, ctypes.byref(label), ctypes.sizeof(label) + adv.GetLengthSid(medium)):
            fail("SetTokenInformation")  # TokenIntegrityLevel
        # Objects the new process creates (its own process and thread too) are owned by and
        # open to the Administrators group by default here, which is now switched off: make
        # them the user's, as in a normal UAC token, or the process can't even start.
        need = W.DWORD()
        adv.GetTokenInformation(lim, 1, None, 0, ctypes.byref(need))  # TokenUser
        ubuf = ctypes.create_string_buffer(need.value)
        if not adv.GetTokenInformation(lim, 1, ubuf, need, ctypes.byref(need)):
            fail("GetTokenInformation")
        user_sid = ctypes.c_void_p.from_buffer(ubuf).value
        owner = ctypes.c_void_p(user_sid)
        if not adv.SetTokenInformation(lim, 4, ctypes.byref(owner), ctypes.sizeof(owner)):  # TokenOwner
            fail("SetTokenInformation(owner)")
        sid_str = W.LPWSTR()
        if not adv.ConvertSidToStringSidW(user_sid, ctypes.byref(sid_str)):
            fail("ConvertSidToStringSid")
        sddl = f"D:(A;;GA;;;{sid_str.value})(A;;GA;;;SY)"
        k32.LocalFree(ctypes.cast(sid_str, ctypes.c_void_p))
        sd = ctypes.c_void_p()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            fail("ConvertStringSecurityDescriptor")
        sids.append(sd)
        present, defaulted, dacl = W.BOOL(), W.BOOL(), ctypes.c_void_p()
        adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
        if not adv.SetTokenInformation(lim, 6, ctypes.byref(dacl), ctypes.sizeof(dacl)):  # TokenDefaultDacl
            fail("SetTokenInformation(default DACL)")

        sa = SA(ctypes.sizeof(SA), None, True)
        fd, out_path = tempfile.mkstemp(prefix="harvest-app-", suffix=".log")
        os.close(fd)
        out = k32.CreateFileW(out_path, 0x40000000, 3, ctypes.byref(sa), 2, 0x80, None)  # GENERIC_WRITE, CREATE_ALWAYS
        nul = k32.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(sa), 3, 0x80, None)      # GENERIC_READ, OPEN_EXISTING
        if out in (None, W.HANDLE(-1).value) or nul in (None, W.HANDLE(-1).value):
            fail("CreateFile")
        handles += [W.HANDLE(out), W.HANDLE(nul)]
        si = SI(cb=ctypes.sizeof(SI), dwFlags=0x100, hStdInput=nul, hStdOutput=out, hStdError=out)  # STARTF_USESTDHANDLES
        pi = PI()
        block = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
        envbuf = ctypes.create_unicode_buffer(block, len(block))
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
        # CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
        if not adv.CreateProcessAsUserW(lim, None, line, None, None, True, 0x400 | NO_WINDOW, envbuf, None,
                                        ctypes.byref(si), ctypes.byref(pi)):
            fail("CreateProcessAsUser")
        handles += [W.HANDLE(pi.hProcess), W.HANDLE(pi.hThread)]
        timed_out = k32.WaitForSingleObject(pi.hProcess, timeout * 1000) == 0x102
        if timed_out:
            k32.TerminateProcess(pi.hProcess, 1)
            k32.WaitForSingleObject(pi.hProcess, 10000)
        code = W.DWORD()
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        for h in handles[2:]:
            k32.CloseHandle(h)
        handles = handles[:2]
        try:
            raw = Path(out_path).read_bytes()
            os.remove(out_path)
        except OSError:
            raw = b""
        text = raw.decode("utf-8", "replace")
        if timed_out:
            return -1, text + f"\ntimed out after {timeout // 60} minutes"
        return ctypes.c_int32(code.value).value, text
    finally:
        for h in handles:
            k32.CloseHandle(h)
        for sid in sids:
            k32.LocalFree(sid)


def _run(cmd: list[str], timeout: int = 120, env: dict | None = None) -> tuple[int, str]:
    e = os.environ.copy()
    e.update(env or {})
    if WINDOWS and _elevated():
        try:
            return _run_limited(cmd, timeout, e)
        except OSError as ex:
            return -1, str(ex)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout, env=e, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout // 60} minutes"
    except OSError as ex:
        return -1, str(ex)


BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1", "NONINTERACTIVE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1"}
WINGET_COMMON = ["--source", "winget", "--accept-source-agreements", "--disable-interactivity"]


def _clean_lines(text: str) -> list[str]:
    """Drop winget's progress spinners and bars."""
    out = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.split("\r")[-1].rstrip()
        if not line.strip() or re.fullmatch(r"\s*[-\\|/]\s*", line) or re.search(r"[█▒]{3,}", line):
            continue
        out.append(line)
    return out


def _tail(text: str, n: int = 12) -> str:
    return "\n".join(_clean_lines(text)[-n:])[-1500:]


# --- what's installed -------------------------------------------------------------

def installed() -> dict:
    """Installed package ids per manager (for the dashboard's "installed" badges)."""
    out: dict[str, list[str]] = {}
    for m in managers():
        try:
            if m == "winget":
                # Not tempfile.mkdtemp: that folder is private to its creator (the Administrators
                # group when Harvest has an admin token), so the standard-user winget couldn't write.
                d = Path(tempfile.gettempdir()) / f"harvest-apps-{os.getpid()}"
                d.mkdir(exist_ok=True)
                f = d / "export.json"
                try:
                    _run([_winget(), "export", "-o", str(f), *WINGET_COMMON], timeout=180)
                    data = json.loads(f.read_text(encoding="utf-8-sig")) if f.exists() else {}
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                out[m] = sorted({p.get("PackageIdentifier") for s in data.get("Sources", []) for p in s.get("Packages", [])
                                 if p.get("PackageIdentifier")})
            elif m == "brew":
                ids = []
                for flag, prefix in (("--formula", ""), ("--cask", "cask:")):
                    code, text = _run([_brew(), "list", flag, "-1"], timeout=120, env=BREW_ENV)
                    if code == 0:
                        ids += [prefix + x.strip() for x in text.splitlines() if x.strip() and not x.startswith("==>")]
                out[m] = sorted(ids)
            elif m == "flatpak":
                code, text = _run(["flatpak", "list", "--app", "--columns=application"], timeout=120)
                out[m] = sorted({x.strip() for x in text.splitlines() if code == 0 and x.strip()})
        except Exception as e:  # one broken manager shouldn't hide the others
            out[m] = []
            print(f"apps: could not list {m} packages: {e}")
    return out


# --- search -------------------------------------------------------------------------

def _winget_table(text: str) -> list[dict]:
    lines = _clean_lines(text)
    head = next((i for i, l in enumerate(lines) if re.match(r"^-{10,}", l.strip())), None)
    if head is None or head == 0:
        return []
    h = lines[head - 1]
    cols = [(name, h.find(name)) for name in ("Name", "Id", "Version", "Match", "Source")]
    cols = [(n, i) for n, i in cols if i >= 0]
    rows = []
    for l in lines[head + 1:]:
        cells = {}
        for k, (name, start) in enumerate(cols):
            end = cols[k + 1][1] if k + 1 < len(cols) else None
            cells[name] = l[start:end].strip() if end else l[start:].strip()
        if cells.get("Id") and ID_OK["winget"].match(cells["Id"]):
            rows.append(cells)
    return rows


def search(query: str, manager: str | None = None) -> dict:
    query = " ".join(str(query or "").split())[:60]
    if not query:
        return {"error": "type something to search for"}
    results = []
    for m in managers():
        if manager and m != manager:
            continue
        if m == "winget":
            code, text = _run([_winget(), "search", query, "--count", "25", *WINGET_COMMON], timeout=120)
            for r in _winget_table(text):
                results.append({"manager": m, "id": r["Id"], "name": r.get("Name") or r["Id"], "version": r.get("Version") or ""})
        elif m == "brew":
            for flag in ("--formula", "--cask"):
                code, text = _run([_brew(), "search", flag, query], timeout=120, env=BREW_ENV)
                for x in text.splitlines():
                    x = x.strip()
                    if code == 0 and x and not x.startswith("==>") and ID_OK["brew"].match(x):
                        cask = flag == "--cask"
                        results.append({"manager": m, "id": ("cask:" if cask else "") + x, "name": x + (" (app)" if cask else ""), "version": ""})
        elif m == "flatpak":
            code, text = _run(["flatpak", "search", "--columns=application,name,version", query], timeout=120)
            for x in text.splitlines():
                parts = x.split("\t")
                if code == 0 and len(parts) >= 2 and ID_OK["flatpak"].match(parts[0].strip()):
                    results.append({"manager": m, "id": parts[0].strip(), "name": parts[1].strip(), "version": (parts[2] if len(parts) > 2 else "").strip()})
    return {"query": query, "results": results[:40], "managers": managers()}


# --- install / uninstall ----------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass


def _check(manager: str, pkg: str) -> str | None:
    if manager not in managers():
        if _elevated():
            return "Harvest is running as root on this machine, and installs never use admin rights"
        return {"winget": "winget (App Installer) isn't available on this PC",
                "brew": "Homebrew isn't installed on this PC (install it from brew.sh first)",
                "flatpak": "Flatpak isn't installed on this PC"}.get(manager, f"unknown package manager {manager!r}")
    bare = pkg[5:] if manager == "brew" and pkg.startswith("cask:") else pkg
    if not ID_OK[manager].match(bare):
        return "that doesn't look like a package id"
    return None


def fresh_path(current: str) -> str:
    """PATH with folders that new installs add (they only reach programs started later)."""
    parts = [p for p in (current or "").split(os.pathsep) if p]
    extra = []
    if WINDOWS:
        try:
            import winreg
            for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(root, key) as k:
                        v = winreg.QueryValueEx(k, "Path")[0]
                        extra += [os.path.expandvars(x) for x in v.split(";") if x]
                except OSError:
                    pass
        except ImportError:
            pass
        extra.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"))
    else:
        b = _brew()
        if b:
            extra += [os.path.dirname(b), os.path.join(os.path.dirname(os.path.dirname(b)), "sbin")]
    seen = {os.path.normcase(p) for p in parts}
    for p in extra:
        if p and os.path.normcase(p) not in seen and os.path.isdir(p):
            parts.append(p)
            seen.add(os.path.normcase(p))
    return os.pathsep.join(parts)


WINGET_FAIL = {
    "No applicable installer": "This app has no per-user installer: it needs admin rights, which Harvest never uses.",
    "No package found matching": "No app with that id in winget.",
    "requires administrator": "This app needs admin rights, which Harvest never uses.",
    "Installer failed with exit code": "The app's own installer failed.",
    "could not drop admin rights": "Harvest couldn't start the install without admin rights on this PC.",
    "could not let this user": "Harvest couldn't start the install without admin rights on this PC.",
}


def install(manager: str, pkg: str, uninstall: bool = False) -> dict:
    err = _check(manager, pkg)
    if err:
        return {"ok": False, "note": err}
    verb = "uninstall" if uninstall else "install"
    t0 = time.time()
    if manager == "winget":
        cmd = [_winget(), verb, "--id", pkg, "--exact", "--scope", "user", "--silent", *WINGET_COMMON]
        if not uninstall:
            cmd.append("--accept-package-agreements")
        code, text = _run(cmd, timeout=INSTALL_TIMEOUT)
        low = text.lower()
        ok = code == 0
        if not ok and not uninstall and ("already installed" in low or "no available upgrade" in low or "no newer package" in low):
            ok, note = True, "already installed"
        elif ok:
            note = "uninstalled" if uninstall else "installed"
        else:
            note = next((v for k, v in WINGET_FAIL.items() if k.lower() in low), f"winget stopped with code {code}")
    elif manager == "brew":
        cask = pkg.startswith("cask:")
        name = pkg[5:] if cask else pkg
        code, text = _run([_brew(), verb, "--cask" if cask else "--formula", name], timeout=INSTALL_TIMEOUT, env=BREW_ENV)
        ok = code == 0
        note = ("uninstalled" if uninstall else "installed") if ok else (
            "This app needs your password to install (admin rights), which Harvest never uses."
            if "sudo" in text.lower() or "password" in text.lower() else f"Homebrew stopped with code {code}")
    else:  # flatpak
        if not uninstall:
            _run(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo"], timeout=120)
            cmd = ["flatpak", "install", "--user", "-y", "--noninteractive", "flathub", pkg]
        else:
            cmd = ["flatpak", "uninstall", "--user", "-y", "--noninteractive", pkg]
        code, text = _run(cmd, timeout=INSTALL_TIMEOUT)
        ok = code == 0
        note = ("uninstalled" if uninstall else "installed") if ok else f"Flatpak stopped with code {code}"
    if ok:
        st = _load_state()
        by = st.setdefault("installed", {})
        key = f"{manager}:{pkg}"
        if uninstall:
            by.pop(key, None)
        else:
            by.setdefault(key, {"at": int(time.time())})
        _save_state(st)
        os.environ["PATH"] = fresh_path(os.environ.get("PATH", ""))  # so tasks started from now on find it
    return {"ok": ok, "note": note, "manager": manager, "id": pkg, "action": verb,
            "seconds": round(time.time() - t0), "output": _tail(text)}


def scan() -> dict:
    st = _load_state()
    return {"managers": managers(), "installed": installed(), "elevated": _elevated(),
            "by_harvest": sorted((st.get("installed") or {}).keys()),
            "os": "windows" if WINDOWS else "mac" if MAC else "linux"}


def run(command: dict) -> dict:
    """Entry point for a command from the server (kind apps_scan | apps_search | apps_install | apps_uninstall)."""
    kind = command.get("kind")
    if kind == "apps_scan":
        return scan()
    if kind == "apps_search":
        return search(command.get("query"), command.get("manager"))
    if kind in ("apps_install", "apps_uninstall"):
        r = install(str(command.get("manager") or ""), str(command.get("package") or ""), uninstall=kind == "apps_uninstall")
        return r if r["ok"] else {**r, "error": r["note"]}
    return {"error": f"unknown command {kind!r}"}
