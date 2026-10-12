#!/usr/bin/env python3
"""natron-mcp installer: deploys the MCP bridge + autoload into a Natron
installation, and (optionally) sets up the MCP server venv.

Targets
-------
1. Portable Natron install (this fork's layout, upstream-portable also works):
       <root>/App/Natron.exe            (fork GUI)
       <root>/Natron.exe                (upstream)
       <root>/Renderer/NatronRenderer.exe
   AppImage-style layout (extracted AppImage / Linux portable):
       <root>/usr/bin/{Natron, NatronRenderer}
   (point at the extracted squashfs-root folder; a bare .AppImage file is
   rejected with an extract-first hint)
   Deploys:
       <root>/Resources/natron-mcp/bridge/natron_mcp_bridge/...
       <root>/Plugins/PyPlugs/natron-mcp/init.py      (all modes: path setup)
       <root>/Plugins/PyPlugs/natron-mcp/initGui.py   (GUI: starts the bridge)
       <root>/Resources/natron-mcp/manifest.json      (what remove() undoes)
   (AppImage layout: same, under <root>/usr/)

2. Natron fork build tree:
       <root>/tools/win-build/06-install.sh
   Injects a managed block into 06-install.sh so every build stages the bridge
   and autoload scripts into $I. The generated autoload scripts locate the
   bridge at runtime (no absolute paths), so the staged install is relocatable.

Why Plugins/PyPlugs: Natron runs init.py (all modes) and initGui.py (GUI only)
found recursively under its non-OFX plugin search paths (AppManager.cpp
getAllNonOFXPluginsPaths / findAllScriptsRecursive), and adds every such
subdirectory to sys.path. A directory containing ONLY init scripts produces no
PyPlug candidates and no error noise; the bridge itself lives in Resources/,
which is never scanned.

Usage
-----
  python install.py                       interactive
  python install.py --natron <path>       non-interactive target guess
  python install.py --action install|remove|repair --natron <path> [--yes]
  python install.py --with-server         also create the MCP server venv
  python install.py --dry-run             show what would happen
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

VERSION = "0.1.0"

MARK_BEGIN = "# >>> natron-mcp staging (managed block, maintained by tools/mcp/install.py) >>>"
MARK_END = "# <<< natron-mcp staging <<<"

HOME_MCP_DIR = Path.home() / ".Natron" / "natron-mcp"
POINTER_FILE = HOME_MCP_DIR / "bridge-dir.txt"
REGISTRY_FILE = HOME_MCP_DIR / "install.json"

# Package layout: install.py sits at the package root next to bridge/ and server/.
PKG_ROOT = Path(__file__).resolve().parent
BRIDGE_SRC = PKG_ROOT / "bridge" / "natron_mcp_bridge"


# --------------------------------------------------------------------------- #
# small utils
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print("  " + msg)


def warn(msg: str) -> None:
    print("  [!] " + msg)


def die(msg: str, code: int = 2) -> None:
    print("[x] " + msg)
    raise SystemExit(code)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ask(prompt: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    try:
        answer = input(prompt + suffix + ": ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_bool(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = ask("%s (%s)" % (prompt, hint), "y" if default else "n").lower()
    return raw in ("y", "yes", "1", "true")


# --------------------------------------------------------------------------- #
# target classification
# --------------------------------------------------------------------------- #

def classify_target(p: Path) -> Optional[tuple[str, Path]]:
    """Return ("portable", root) or ("build_tree", root) or None."""
    p = Path(p).resolve()
    if p.is_file():
        # user pointed at an exe
        parent = p.parent
        if p.name.lower() in ("natron.exe", "natronrenderer.exe", "natron"):
            # GUI exe lives in App/ (fork) or root (upstream); renderer in Renderer/
            root = parent.parent if parent.name.lower() in ("app", "renderer") else parent
            return ("portable", root)
        return None
    if not p.is_dir():
        return None
    if ((p / "tools" / "win-build" / "06-install.sh").exists()
            or (p / "tools" / "mcp" / "install.py").exists() and (p / "Natron").is_dir()):
        return ("build_tree", p)
    # AppImage-style layout (extracted AppImage / Linux portable): binaries
    # under usr/bin, Resources/Plugins siblings of bin.
    if is_appimage_layout(p):
        return ("portable", p)
    if ((p / "App" / "Natron.exe").exists() or (p / "Natron.exe").exists()
            or (p / "App" / "Natron").exists() or (p / "Natron").exists()
            or (p / "Renderer" / "NatronRenderer.exe").exists()):
        return ("portable", p)
    # last resort: exe directly inside (upstream Linux/Mac bundles)
    if any(x.name.lower().startswith("natron") and x.is_file() and x.suffix.lower() in (".exe", "")
           for x in p.iterdir() if x.is_file()):
        return ("portable", p)
    return None


def is_appimage_layout(root: Path) -> bool:
    """True for extracted-AppImage trees: <root>/usr/bin/{Natron,NatronRenderer}."""
    return ((root / "usr" / "bin" / "Natron").exists()
            or (root / "usr" / "bin" / "NatronRenderer").exists())


def app_prefix(root: Path) -> Path:
    """Directory the portable layout hangs Resources/Plugins off:
    <root>/usr for AppImage-style trees, <root> for Windows/upstream layouts."""
    return root / "usr" if is_appimage_layout(root) else root


def root_of_exe(exe: Path) -> Path:
    parent = exe.parent
    if parent.name.lower() not in ("app", "renderer", "bin"):
        return parent
    root = parent.parent
    # AppImage layout: usr/bin/<exe> -> root is the dir containing usr/
    return root.parent if root.name.lower() == "usr" else root


# --------------------------------------------------------------------------- #
# install state
# --------------------------------------------------------------------------- #

def manifest_path(root: Path) -> Path:
    return app_prefix(root) / "Resources" / "natron-mcp" / "manifest.json"


def read_manifest(root: Path) -> Optional[dict[str, Any]]:
    mp = manifest_path(root)
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def detect_state(root: Path) -> tuple[str, Optional[dict[str, Any]]]:
    """absent | partial | installed (+ manifest if any)."""
    man = read_manifest(root)
    if man:
        return ("installed", man)
    pres = app_prefix(root)
    bridges = (pres / "Resources" / "natron-mcp" / "bridge" / "natron_mcp_bridge").is_dir()
    inits = (pres / "Plugins" / "PyPlugs" / "natron-mcp" / "initGui.py").exists()
    if bridges or inits:
        return ("partial", None)
    return ("absent", None)


# --------------------------------------------------------------------------- #
# generated init scripts
# --------------------------------------------------------------------------- #

# Locate-the-bridge prelude, shared by both generated scripts. Tokens:
#   @VERSION@    installer version
#   @HARDCODED@  either the hardcoded-path block or "" (relocatable installs)
_LOCATE = r'''# --- natron-mcp bridge (generated by natron-mcp install v@VERSION@) ---
# Locates the staged bridge without relying on __file__ (Natron runs init
# scripts via PyRun_SimpleString, so __file__ does not exist):
#   1. NATRON_MCP_BRIDGE_DIR env override
#   2. relative to this autoload: Natron runs init.py/initGui.py from
#      <root>/Plugins/PyPlugs/natron-mcp and adds that dir to sys.path, so we
#      recognize it and derive <root>/Resources/natron-mcp/bridge
#   3. hardcoded path embedded at install time (if any)
#   4. last-resort home fallback (~/.Natron/natron-mcp/bridge)
import os as _os
import sys as _sys


def _natron_mcp_locate_bridge():
    cands = []
    env = _os.environ.get("NATRON_MCP_BRIDGE_DIR")
    if env:
        cands.append(env)
    for _p in list(_sys.path):
        try:
            _pp = _os.path.abspath(_p).replace("\\", "/")
        except Exception:
            continue
        if _pp.endswith("Plugins/PyPlugs/natron-mcp"):
            cands.append(_os.path.abspath(_os.path.join(
                _pp, "..", "..", "..", "Resources", "natron-mcp", "bridge")))
@HARDCODED@    cands.append(_os.path.expanduser("~/.Natron/natron-mcp/bridge"))
    for _c in cands:
        if _c and _os.path.isfile(_os.path.join(_c, "natron_mcp_bridge", "__init__.py")):
            return _c
    return None


_bridge_parent = _natron_mcp_locate_bridge()
'''

_HARDCODED_LINE = r'''    _hc = @PATH@
    if _hc and _os.path.isfile(_os.path.join(_hc, "natron_mcp_bridge", "__init__.py")):
        cands.append(_hc)
'''

_INITGUI_TEMPLATE = _LOCATE + r'''
if _bridge_parent:
    if _bridge_parent not in _sys.path:
        _sys.path.insert(0, _bridge_parent)
    try:
        import natron_mcp_bridge as _mcp
    except Exception as _exc:
        print('[natron-mcp] bridge import failed: %r' % (_exc,))
    else:
        try:
            _mcp.bind_app(app)  # noqa: F821  (PyRun_SimpleString: __main__ globals)
        except NameError:
            try:
                import NatronEngine
                _mcp.bind_app(NatronEngine.natron.getInstance(0))
            except Exception:
                pass
        if _mcp.get_server() is None:
            _srv = _mcp.start_gui()
            try:
                app.writeToScriptEditor('[natron-mcp] bridge listening on port %d (session %s)' % (_srv.port, _srv.session_id))  # noqa: F821
            except Exception:
                print('[natron-mcp] bridge listening on port', _srv.port)
        else:
            print('[natron-mcp] bridge already running on port', _mcp.get_server().port)
else:
    print('[natron-mcp] bridge not found (checked env, PyPlugs layout, install-time path, home fallback)')
'''

_INIT_TEMPLATE = _LOCATE + r'''
if _bridge_parent and _bridge_parent not in _sys.path:
    _sys.path.insert(0, _bridge_parent)
# No server is started here on purpose: init.py also runs in NatronRenderer
# project runs, where blocking on the bridge would wedge the render. GUI
# autoload lives in initGui.py next to this file; headless hosts are spawned
# and driven by the MCP server (sessions_spawn), which sets
# NATRON_MCP_BRIDGE_DIR and passes the bootstrap script itself.
'''


def _render(template: str, bridge_dir: Optional[str]) -> str:
    if bridge_dir:
        hc = _HARDCODED_LINE.replace("@PATH@", repr(bridge_dir))
    else:
        hc = ""
    return template.replace("@VERSION@", VERSION).replace("@HARDCODED@", hc)


def build_initgui_py(bridge_dir: Optional[str] = None) -> str:
    """GUI autoload: starts the bridge (non-blocking; Qt dispatcher drains ops)."""
    return _render(_INITGUI_TEMPLATE, bridge_dir)


def build_init_py(bridge_dir: Optional[str] = None) -> str:
    """Headless-safe autoload: path setup only.

    Headless sessions are spawned and driven by the MCP server
    (sessions_spawn -> NatronRenderer/natron-python + bootstrap scripts), so no
    server is started here; blocking in init.py would wedge project runs.
    """
    return _render(_INIT_TEMPLATE, bridge_dir)


# --------------------------------------------------------------------------- #
# portable install / remove
# --------------------------------------------------------------------------- #

def bridge_files() -> list[Path]:
    files = sorted(BRIDGE_SRC.rglob("*.py"))
    if not files:
        die("bridge source not found at %s" % BRIDGE_SRC)
    return files


def install_portable(root: Path, dry: bool = False, with_server: Optional[bool] = None,
                     assume_yes: bool = False) -> dict[str, Any]:
    pres = app_prefix(root)
    res_mcp = pres / "Resources" / "natron-mcp"
    bridge_dst = res_mcp / "bridge"
    init_dir = pres / "Plugins" / "PyPlugs" / "natron-mcp"
    bridge_dst_abs = bridge_dst  # absolute (embedded as fallback hint)

    files: list[str] = []
    if dry:
        log("[dry-run] would copy %d bridge files -> %s" % (len(bridge_files()), bridge_dst))
        log("[dry-run] would write %s and %s" % (init_dir / "init.py", init_dir / "initGui.py"))
    else:
        if bridge_dst.exists():
            shutil.rmtree(bridge_dst)
        bridge_dst.mkdir(parents=True, exist_ok=True)
        for f in bridge_files():
            rel = f.relative_to(BRIDGE_SRC)
            dst = bridge_dst / "natron_mcp_bridge" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            files.append(str(dst.relative_to(root)))

        init_dir.mkdir(parents=True, exist_ok=True)
        (init_dir / "init.py").write_text(build_init_py(str(bridge_dst_abs)), encoding="utf-8")
        (init_dir / "initGui.py").write_text(build_initgui_py(str(bridge_dst_abs)), encoding="utf-8")
        files.append(str((init_dir / "init.py").relative_to(root)))
        files.append(str((init_dir / "initGui.py").relative_to(root)))

        register_install(root, bridge_dst)
        write_pointer(bridge_dst)

    # Server decision. The bridge inside Natron is only the in-host endpoint;
    # MCP clients connect to the separate server process. --with-server/--no-server
    # force it; otherwise detect an existing server and ask (interactive) or
    # default to installing one (unattended fresh install).
    srv = detect_server(root)
    if srv["cli"]:
        if with_server:
            print("  [i] an MCP server is already usable (%s: %s); not creating another"
                  % (srv["where"], srv["cli"]))
        with_server = False if with_server is None else with_server
    elif with_server is None:
        if dry:
            with_server = True
        elif assume_yes:
            with_server = True
        else:
            print("  The bridge runs inside Natron, but your AI client (Claude Desktop,")
            print("  Cursor, ...) talks to the separate MCP SERVER. None was detected.")
            with_server = ask_bool("Set up the MCP server now (recommended)?", True)

    venv_info = None
    if with_server:
        venv_info = setup_server_venv(root, dry=dry, assume_yes=assume_yes)

    if not dry:
        # Manifest is written LAST so it also records the server venv; remove()
        # uses this to uninstall everything we put here.
        manifest = {
            "package": "natron-mcp",
            "version": VERSION,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": "portable",
            "natron_root": str(root),
            "bridge_dir": str(bridge_dst),
            "server_venv": str(venv_cli_path(root).relative_to(root).as_posix())
                           if venv_cli_path(root).exists() else None,
            "files": files,
        }
        res_mcp.mkdir(parents=True, exist_ok=True)
        manifest_path(root).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print()
    print("[ok] natron-mcp %s installed into: %s" % (VERSION, root))
    print("     bridge:  %s" % (bridge_dst if not dry else "(dry-run)"))
    print("     autoload: %s" % (init_dir.relative_to(root) / "{init.py,initGui.py}"))
    if not dry:
        print("     Start Natron; the Script Editor shows '[natron-mcp] bridge listening on port N'.")
    print_server_status(root, venv_info, srv, dry)
    return {"ok": True}


def print_server_status(root: Path, venv_info: Optional[dict[str, Any]],
                        srv: dict[str, Any], dry: bool = False) -> None:
    print()
    print("  MCP server (what your AI client connects to):")
    if venv_info and venv_info.get("cli"):
        if dry:
            print("    [dry-run] would create a server venv in <root>\\natron-mcp\\server-venv")
            return
        print("    created: %s" % venv_info["cli"])
        print_client_config(venv_info)
    elif srv.get("cli"):
        print("    found:   %s  (%s)" % (srv["cli"], srv["where"]))
        print("    Point your MCP client at this command; no further setup needed.")
        print("    Example Claude Desktop config:")
        print(json.dumps({"mcpServers": {"natron": {"command": srv["cli"]}}}, indent=2))
    else:
        print("    NOT set up -- the bridge alone does not talk to AI clients.")
        print("    Do one of these later:")
        print("      a) re-run this installer with --with-server (self-contained venv), or")
        print("      b) pip install --user %s   (or pipx)" % (PKG_ROOT / "server"))
    print()


def rmtree_force(path: Path) -> bool:
    """rmtree that clears Windows read-only bits; returns False if files were
    locked (e.g. the MCP server is running from the venv)."""
    import stat
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                os.chmod(os.path.join(dirpath, name), stat.S_IWRITE)
            except OSError:
                pass
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        warn("could not fully remove %s (%s) -- close anything running from it "
             "and delete it manually" % (path, exc))
        return False


def remove_venv(root: Path, dry: bool = False) -> bool:
    """Delete the installer-owned server venv at <root>/natron-mcp/server-venv."""
    venv_root = root / "natron-mcp"
    gone = True
    if (venv_root / "server-venv").exists():
        if dry:
            log("[dry-run] would delete %s" % (venv_root / "server-venv"))
        else:
            print("  removing MCP server venv %s" % (venv_root / "server-venv"))
            gone = rmtree_force(venv_root / "server-venv")
    if venv_root.exists() and not any(venv_root.iterdir()):
        if dry:
            log("[dry-run] would rmdir %s" % venv_root)
        else:
            venv_root.rmdir()
    return gone


def remove_portable(root: Path, dry: bool = False, keep_venv: bool = False) -> dict[str, Any]:
    man = read_manifest(root)
    removed = 0
    if man:
        # runtime-compiled bytecode caches are not manifest entries; purge them
        # FIRST so the parent walk-up below can collapse the emptied dirs
        pres = app_prefix(root)
        if not dry:
            for base in (pres / "Resources" / "natron-mcp",
                         pres / "Plugins" / "PyPlugs" / "natron-mcp"):
                if base.exists():
                    for pc in base.rglob("__pycache__"):
                        shutil.rmtree(pc, ignore_errors=True)
                        removed += 1
        files = man.get("files", [])
        for rel in reversed(files):
            f = root / rel
            if f.exists():
                if dry:
                    log("[dry-run] would delete %s" % f)
                else:
                    f.unlink()
                removed += 1
            d = f.parent
            while d != root and d.exists() and not any(d.iterdir()):
                if dry:
                    log("[dry-run] would rmdir %s" % d)
                    break
                d.rmdir()
                d = d.parent
        if not dry:
            if manifest_path(root).exists():
                manifest_path(root).unlink()
                removed += 1
            res_mcp = pres / "Resources" / "natron-mcp"
            if res_mcp.exists() and not any(res_mcp.iterdir()):
                res_mcp.rmdir()
            # the server venv lives outside Resources/ and is recorded in the
            # manifest (or found in its fixed location); repair keeps it so
            # the server is not re-downloaded
            if not keep_venv and (man.get("server_venv") or (root / "natron-mcp" / "server-venv").exists()):
                remove_venv(root, dry=False)
    else:
        # no manifest: sweep known locations (partial installs)
        targets = [
            root / "Resources" / "natron-mcp",
            root / "Plugins" / "PyPlugs" / "natron-mcp",
            root / "usr" / "Resources" / "natron-mcp",
            root / "usr" / "Plugins" / "PyPlugs" / "natron-mcp",
        ]
        for t in targets:
            if t.exists():
                if dry:
                    log("[dry-run] would rmtree %s" % t)
                else:
                    shutil.rmtree(t)
                removed += 1
        if not keep_venv:
            remove_venv(root, dry=dry)

    if not dry:
        unregister_install(root)

    print()
    print("[ok] removed %d file(s) from %s" % (removed, root))
    return {"ok": True, "removed": removed}


def repair_portable(root: Path, dry: bool = False, with_server: Optional[bool] = None,
                    assume_yes: bool = False) -> dict[str, Any]:
    if not dry:
        # keep_venv: the server venv survives repairs (no re-download)
        remove_portable(root, keep_venv=True)
    return install_portable(root, dry=dry, with_server=with_server, assume_yes=assume_yes)


# --------------------------------------------------------------------------- #
# registry + pointer (used by the MCP server to spawn hosts)
# --------------------------------------------------------------------------- #

def register_install(root: Path, bridge_dir: Path) -> None:
    gui = next((root / a / b for a, b in
                (("App", "Natron.exe"), ("App", "Natron"), ("", "Natron.exe"), ("", "Natron"),
                 ("usr/bin", "Natron"))
                if (root / a / b).exists()), None)
    ren = next((root / a / b for a, b in
                (("Renderer", "NatronRenderer.exe"), ("Renderer", "NatronRenderer"), ("", "NatronRenderer"),
                 ("usr/bin", "NatronRenderer"))
                if (root / a / b).exists()), None)
    try:
        HOME_MCP_DIR.mkdir(parents=True, exist_ok=True)
        reg: dict[str, Any] = {"installs": []}
        if REGISTRY_FILE.exists():
            try:
                reg = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reg = {"installs": []}
        entries = [e for e in reg.get("installs", []) if e.get("root") != str(root)]
        entries.insert(0, {
            "root": str(root),
            "bridge_dir": str(bridge_dir),
            "gui_bin": str(gui) if gui else None,
            "renderer_bin": str(ren) if ren else None,
            "version": VERSION,
        })
        reg["installs"] = entries
        tmp = REGISTRY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
        os.replace(tmp, REGISTRY_FILE)
    except OSError as exc:
        warn("could not update install registry (%s); server spawn may need "
             "NATRON_MCP_BRIDGE_DIR/NATRON_MCP_GUI_BIN env vars" % exc)


def unregister_install(root: Path) -> None:
    root = Path(root)
    try:
        if REGISTRY_FILE.exists():
            reg = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            reg["installs"] = [e for e in reg.get("installs", []) if e.get("root") != str(root)]
            tmp = REGISTRY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
            os.replace(tmp, REGISTRY_FILE)
        if POINTER_FILE.exists() and read_pointer().resolve() == (app_prefix(root) / "Resources" / "natron-mcp" / "bridge").resolve():
            POINTER_FILE.unlink()
    except (OSError, ValueError):
        pass


def read_pointer() -> Path:
    if POINTER_FILE.exists():
        try:
            return Path(POINTER_FILE.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return Path()


def write_pointer(bridge_dir: Path) -> None:
    try:
        HOME_MCP_DIR.mkdir(parents=True, exist_ok=True)
        POINTER_FILE.write_text(str(bridge_dir), encoding="utf-8")
    except OSError as exc:
        warn("could not write bridge pointer (%s)" % exc)


# --------------------------------------------------------------------------- #
# build-tree injection (fork repo)
# --------------------------------------------------------------------------- #

_BLOCK = r'''
# >>> natron-mcp staging (managed block, maintained by tools/mcp/install.py) >>>
# Stage the MCP bridge and its autoload scripts into every install produced by
# this script. The autoload scripts self-locate the bridge at runtime, so the
# result stays relocatable.
if [ -d "$NATRON_ROOT/tools/mcp/bridge/natron_mcp_bridge" ]; then
  log "natron-mcp: staging bridge + autoload"
  mkdir -p "$I/Resources/natron-mcp/bridge" "$I/Plugins/PyPlugs/natron-mcp"
  cp -r "$NATRON_ROOT/tools/mcp/bridge/natron_mcp_bridge" "$I/Resources/natron-mcp/bridge/"
  rm -rf "$I/Resources/natron-mcp/bridge/natron_mcp_bridge/__pycache__"
  for _f in "$NATRON_ROOT/tools/mcp/installer-assets/init"/*.py; do
    [ -e "$_f" ] && cp "$_f" "$I/Plugins/PyPlugs/natron-mcp/"
  done
  ok "natron-mcp staged"
fi
# <<< natron-mcp staging <<<
'''


def write_installer_assets(dry: bool = False) -> Path:
    """Generate relocatable init scripts into installer-assets/init/ (no
    hardcoded paths) so the 06-install.sh block can stage them verbatim."""
    out = PKG_ROOT / "installer-assets" / "init"
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
        (out / "init.py").write_text(build_init_py(None), encoding="utf-8")
        (out / "initGui.py").write_text(build_initgui_py(None), encoding="utf-8")
    return out


def inject_build_tree(root: Path, dry: bool = False) -> bool:
    script = root / "tools" / "win-build" / "06-install.sh"
    if not script.exists():
        die("06-install.sh not found under %s" % (root / "tools" / "win-build"))
    text = script.read_bytes().decode("utf-8")
    if MARK_BEGIN in text:
        log("06-install.sh already contains the natron-mcp block")
        return False
    if dry:
        log("[dry-run] would inject natron-mcp staging block into %s" % script)
        return True
    text = text.rstrip("\n") + "\n" + _BLOCK
    script.write_bytes(text.encode("utf-8"))
    write_installer_assets()
    log("injected staging block into %s" % script)
    log("generated relocatable autoload scripts in %s" % (PKG_ROOT / "installer-assets" / "init"))
    return True


def strip_build_tree(root: Path, dry: bool = False) -> bool:
    script = root / "tools" / "win-build" / "06-install.sh"
    if not script.exists():
        return False
    text = script.read_bytes().decode("utf-8")
    i = text.find(MARK_BEGIN)
    if i < 0:
        return False
    j = text.find(MARK_END, i)
    if j < 0:
        warn("managed block end marker missing in %s; removing to EOF" % script)
        new_text = text[:i].rstrip("\n") + "\n"
    else:
        rest = text[j + len(MARK_END):].lstrip("\n")
        new_text = text[:i].rstrip("\n") + "\n" + rest
    if dry:
        log("[dry-run] would strip natron-mcp block from %s" % script)
        return True
    script.write_bytes(new_text.encode("utf-8"))
    log("stripped staging block from %s" % script)
    return True


def remove_build_tree_assets(root: Path, dry: bool = False) -> None:
    assets = PKG_ROOT / "installer-assets"
    if assets.exists():
        if dry:
            log("[dry-run] would rmtree %s" % assets)
        else:
            shutil.rmtree(assets)


# --------------------------------------------------------------------------- #
# server venv + client config
# --------------------------------------------------------------------------- #

def venv_cli_path(root: Path) -> Path:
    exe_dir = "Scripts" if os.name == "nt" else "bin"
    exe_name = "natron-mcp.exe" if os.name == "nt" else "natron-mcp"
    return root / "natron-mcp" / "server-venv" / exe_dir / exe_name


def detect_server(root: Path) -> dict[str, Any]:
    """Look for an MCP server that is already usable.

    The bridge (inside Natron) is only half of natron-mcp: MCP clients
    (Claude Desktop, Cursor, ...) connect to the *server* process. If a
    usable server already exists, --with-server is unnecessary.
    """
    import shutil as _sh

    info: dict[str, Any] = {"cli": None, "where": None}
    cli = venv_cli_path(root)
    if cli.exists():
        info["cli"], info["where"] = str(cli), "server-venv in this Natron root"
        return info
    which = _sh.which("natron-mcp")
    if which:
        info["cli"], info["where"] = which, "on PATH"
        return info
    for cand in ("python3", "python"):
        exe = _sh.which(cand)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-c", "import natron_mcp"], capture_output=True)
        except OSError:
            continue
        if r.returncode == 0:
            info["cli"], info["where"] = exe + " -m natron_mcp", "importable from %s" % exe
            return info
    return info


def find_python(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit if Path(explicit).exists() else None
    import shutil as _sh
    for cand in ("py", "python3", "python"):
        exe = _sh.which(cand)
        if exe:
            if cand == "py":
                # `py -3` launcher
                try:
                    out = subprocess.run([exe, "-3", "-c", "import sys;print(1)"],
                                         capture_output=True)
                    if out.returncode == 0:
                        return exe + " -3"
                except OSError:
                    continue
            try:
                out = subprocess.run([exe, "-c", "import sys;print(1)"], capture_output=True)
                if out.returncode == 0:
                    return exe
            except OSError:
                continue
    return None


def py_run(python_cmd: str, *args: str, **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(python_cmd.split() + list(args), **kw)


def setup_server_venv(root: Path, dry: bool = False, assume_yes: bool = False,
                      python_cmd: Optional[str] = None) -> Optional[dict[str, Any]]:
    src = PKG_ROOT / "server"
    if not src.is_dir():
        warn("server source not found at %s -- skipping venv setup" % src)
        return None
    venv_dir = root / "natron-mcp" / "server-venv"
    cli = venv_cli_path(root)

    if cli.exists():
        log("server venv already present: %s" % venv_dir)
        return {"venv": str(venv_dir), "cli": str(cli)}

    py = find_python(python_cmd)
    if not py:
        warn("no Python >= 3.10 found on PATH -- install the server manually:")
        warn("    pip install --user %s  (or pipx install from the package root)" % src)
        return None
    probe = py_run(py, "-c", "import sys;print('%d.%d' % sys.version_info[:2])",
                   capture_output=True, text=True)
    ver = probe.stdout.strip()
    if not ver:
        warn("could not determine python version (%s) -- skipping venv setup%s"
             % (py, (": " + probe.stderr.strip()[-200:]) if probe.stderr else ""))
        return None
    major, minor = (int(x) for x in ver.split(".")[:2])
    if (major, minor) < (3, 10):
        warn("found Python %s but the server needs >= 3.10 -- skipping venv setup" % ver)
        return None

    if dry:
        log("[dry-run] would create server venv at %s (python %s)" % (venv_dir, ver))
        return None
    print("  creating server venv at %s (python %s) -- needs network for deps..." % (venv_dir, ver))
    if py_run(py, "-m", "venv", str(venv_dir)).returncode != 0:
        warn("venv creation failed -- install the server manually (see above)")
        return None
    exe_dir = "Scripts" if os.name == "nt" else "bin"
    vpip = str(venv_dir / exe_dir / ("python.exe" if os.name == "nt" else "python"))
    if subprocess.run([vpip, "-m", "pip", "install", str(src)]).returncode != 0:
        warn("pip install failed -- run: %s -m pip install %s" % (vpip, src))
        return None
    return {"venv": str(venv_dir), "cli": str(cli)}


def print_client_config(venv_info: dict[str, Any]) -> None:
    cli = venv_info["cli"]
    cfg = {
        "mcpServers": {
            "natron": {"command": cli, "args": []},
        }
    }
    print("  MCP client config (e.g. Claude Desktop -> claude_desktop_config.json):")
    print(json.dumps(cfg, indent=2))
    print("  HTTP mode instead:            %s --http --port 8765" % cli)
    print("  Headless sessions are spawned by the server itself (sessions_spawn).")


# --------------------------------------------------------------------------- #
# top-level flow
# --------------------------------------------------------------------------- #

def resolve_action(state: str, requested: Optional[str], assume_yes: bool) -> str:
    if requested:
        return requested
    if state == "absent":
        return "install" if assume_yes or ask_bool("natron-mcp is not installed here. Install now?", True) else "quit"
    if state == "installed":
        print("  natron-mcp is already installed here.")
        if assume_yes:
            return "repair"
        raw = ask("Action? (i)nstall=repair / (r)emove / (q)uit [i]", "i").lower()
        return {"i": "repair", "r": "remove", "q": "quit"}.get(raw, "repair")
    print("  A previous/partial natron-mcp installation was found (no manifest).")
    if assume_yes:
        return "repair"
    raw = ask("Action? (r)epair=reinstall / (c)lean remove / (q)uit [r]", "r").lower()
    return {"r": "repair", "c": "remove", "q": "quit"}.get(raw, "repair")


def run(args: argparse.Namespace) -> int:
    target = args.natron
    if not target:
        # sensible default guesses: this repo cloned next to a Natron install
        # (standalone: <repo>/../Natron-install) and the fork's tools/mcp layout
        guesses = [PKG_ROOT.parent / "Natron-install",
                   PKG_ROOT.parent.parent.parent / "Natron-install",   # tools/mcp/x -> <root>/Natron-install
                   PKG_ROOT.parent.parent.parent / "Natron"]
        for g in guesses:
            if classify_target(g):
                target = str(g)
                break

    interactive = sys.stdin.isatty() and not args.yes
    if target:
        c = classify_target(Path(target))
        if not c:
            if str(target).lower().endswith(".appimage"):
                die("not a Natron install: %s\n"
                    "       AppImages are read-only — extract first and point at the\n"
                    "       extracted folder:  ./%s --appimage-extract   ->  ./squashfs-root"
                    % (target, Path(target).name))
            die("not a Natron install or build tree: %s" % target)
        kind, root = c
    else:
        if not interactive:
            die("no --natron target given")
        print("natron-mcp installer v%s" % VERSION)
        print("Where does your Natron live?")
        print("  (the folder that contains Natron.exe / App\\Natron.exe / usr/bin/Natron,")
        print("   the extracted squashfs-root of an AppImage,")
        print("   or a fork build tree containing tools/win-build/06-install.sh)")
        while True:
            raw = ask("Natron location", "")
            c = classify_target(Path(raw)) if raw else None
            if c:
                kind, root = c
                break
            warn("could not recognize that location; try the folder that contains Natron.exe")

    print("  target: %s (%s)" % (root, kind))

    if kind == "build_tree":
        act = args.action
        if act is None:
            if interactive:
                raw = ask("Inject natron-mcp staging into 06-install.sh? (i)nstall / (r)emove / (q)uit [i]", "i").lower()
                act = {"i": "install", "r": "remove", "q": "quit"}.get(raw, "install")
            else:
                act = "install"
        if act == "remove":
            changed = strip_build_tree(root, dry=args.dry_run)
            remove_build_tree_assets(root, dry=args.dry_run)
            print("[ok] build-tree injection removed" if changed else "[ok] no natron-mcp injection present")
            return 0
        inject_build_tree(root, dry=args.dry_run)
        # also offer installing into an existing Natron-install in the same tree
        staged = root / "Natron-install"
        if staged.is_dir() and classify_target(staged):
            print()
            install_root = staged
            state, _ = detect_state(install_root)
            act = resolve_action(state, args.action, args.yes)
            if act == "install":
                install_portable(install_root, dry=args.dry_run, with_server=args.with_server, assume_yes=args.yes)
            elif act == "repair":
                repair_portable(install_root, dry=args.dry_run, with_server=args.with_server, assume_yes=args.yes)
            elif act == "remove":
                remove_portable(install_root, dry=args.dry_run)
        else:
            print("  Run 06-install.sh (or re-run it) to stage natron-mcp into new installs,")
            print("  or point this installer at an existing Natron-install folder directly.")
        return 0

    # portable target
    state, _man = detect_state(root)
    act = resolve_action(state, args.action, args.yes)
    if act == "quit":
        return 1
    if act == "install":
        install_portable(root, dry=args.dry_run, with_server=args.with_server, assume_yes=args.yes)
    elif act == "repair":
        repair_portable(root, dry=args.dry_run, with_server=args.with_server, assume_yes=args.yes)
    elif act == "remove":
        remove_portable(root, dry=args.dry_run)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="natron-mcp installer")
    ap.add_argument("--natron", help="Natron install dir, Natron.exe path, or fork build tree")
    ap.add_argument("--action", choices=("install", "remove", "repair"),
                    help="skip the action menu (non-interactive)")
    ap.add_argument("--with-server", dest="with_server", action="store_true", default=None,
                    help="create the MCP server venv that AI clients connect to "
                         "(default: auto -- skipped if a usable server is detected, "
                         "otherwise offered interactively / created in unattended mode)")
    ap.add_argument("--no-server", dest="with_server", action="store_false",
                    help="never create the server venv (bridge only)")
    ap.add_argument("--python", help="python executable used for the server venv")
    ap.add_argument("--yes", "-y", action="store_true", help="assume default answers")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    args = ap.parse_args()
    try:
        sys.exit(run(args) or 0)
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)


if __name__ == "__main__":
    main()
