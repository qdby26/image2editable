"""Embed used typefaces into a PPTX via PowerPoint COM (Windows only)."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_PP_SAVE_AS_OPENXML = 24
_MSO_TRUE = -1
# Per-instance guards against Open/SaveAs stalling on a hidden modal or
# an add-in/macro event handler on the dedicated automation instance.
_MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3
_PP_ALERTS_NONE = 1
_EMBED_TIMEOUT_ENV = "IMAGE2EDITABLE_FONT_EMBED_TIMEOUT"
_ACTIVATION_TIMEOUT_ENV = "IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT"
# Default in-worker embed watchdog; the env override is validated at call
# time (import must never fail on a bad value).
_EMBED_WATCHDOG_S = 240.0
# The blocking Open/SaveAs section inside the worker is additionally bounded
# by the supervisor; this in-worker watchdog is retained for the attempt-2
# retry semantics.
_CLEANUP_TIMEOUT_S = 15.0
# Microsoft PowerPoint's fixed Application CLSID. DispatchEx by CLSID
# bypasses WPS Office hijacking the "PowerPoint.Application" ProgID —
# WPS registers itself under that ProgID but the CLSID still points at
# real POWERPNT.EXE, and WPS's SaveAs silently ignores the embed flag.
_POWERPOINT_CLSID = "{91493441-5A91-11CF-8700-00AA0060263B}"
EMBED_FONTS_ENV = "IMAGE2EDITABLE_EMBED_FONTS"
_LOGGER = logging.getLogger(__name__)

# Metric-compatible OFL clones for Windows core faces. PowerPoint skips
# embedding system fonts regardless of their fsType bit, so a used core
# typeface is rewritten to its clone before the embed SaveAs. The bundled
# font pool (<repo>/fonts/) ships Arimo and Tinos; the other clones resolve
# only on hosts that already have them installed.
SUBSTITUTES = {
    "Arial": "Arimo",
    "Times New Roman": "Tinos",
    "Calibri": "Carlito",
    "Cambria": "Caladea",
    "Courier New": "Cousine",
    # SimSun ships as TTC — PowerPoint cannot embed multi-face
    # collections. FangSong keeps the CJK serif genre, is a single
    # TTF with editable-embedding fsType, and ships with Windows.
    "SimSun": "FangSong",
}
_XML_FONT_PARTS = (
    "ppt/slides/",
    "ppt/slideLayouts/",
    "ppt/slideMasters/",
    "ppt/notesSlides/",
    "ppt/notesMasters/",
    "ppt/handoutMasters/",
)


def collect_font_usage(pptx_path: str | Path) -> dict:
    path = Path(pptx_path)
    used: set[str] = set()
    embedded: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not (
                name.startswith("ppt/slides/") and name.endswith(".xml")
            ):
                continue
            root = ElementTree.fromstring(archive.read(name))
            for tag in (
                f"{{{_A_NS}}}latin", f"{{{_A_NS}}}ea", f"{{{_A_NS}}}cs"
            ):
                for element in root.iter(tag):
                    typeface = element.get("typeface")
                    if typeface and not typeface.startswith("+"):
                        used.add(typeface)
        if "ppt/presentation.xml" in archive.namelist():
            presentation = ElementTree.fromstring(
                archive.read("ppt/presentation.xml")
            )
            font_list = presentation.find(f"{{{_P_NS}}}embeddedFontLst")
            if font_list is not None:
                for entry in font_list.iter(f"{{{_P_NS}}}font"):
                    typeface = entry.get("typeface")
                    if typeface:
                        embedded.add(typeface)
    return {
        "typefaces_used": sorted(used),
        "embedded": sorted(embedded),
        "not_embedded": sorted(used - embedded),
        "size_bytes": path.stat().st_size,
    }


def _dumb_dispatch(obj):
    import win32com.client.build
    import win32com.client.dynamic

    oleobj = getattr(obj, "_oleobj_", obj)
    return win32com.client.dynamic.CDispatch(
        oleobj, win32com.client.build.DispatchItem()
    )


def _powerpoint_exe_path() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\Classes\CLSID\{_POWERPOINT_CLSID}\LocalServer32",
        )
        raw = winreg.QueryValueEx(key, "")[0]
        exe = Path(raw.split('"')[1] if '"' in raw else raw.split(" /")[0])
        if exe.name.upper() == "POWERPNT.EXE" and exe.exists():
            return exe
    except OSError:
        pass
    for candidate in sorted(
        Path("C:/Program Files").glob(
            "Microsoft Office*/Root/Office*/POWERPNT.EXE"
        )
    ) + sorted(
        Path("C:/Program Files (x86)").glob(
            "Microsoft Office*/Root/Office*/POWERPNT.EXE"
        )
    ):
        if candidate.exists():
            return candidate
    return None


def _powerpoint_pids() -> set[int] | None:
    """PIDs of every running POWERPNT.EXE; None when tasklist fails."""
    try:
        listing = subprocess.run(
            [
                "tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE",
                "/FO", "CSV", "/NH",
            ],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except Exception:
        return None
    if listing.returncode != 0 or listing.stdout is None:
        return None
    pids: set[int] = set()
    for line in listing.stdout.splitlines():
        fields = [f.strip().strip('"') for f in line.split('","')]
        if len(fields) < 2 or fields[0].upper() != "POWERPNT.EXE":
            continue
        try:
            pids.add(int(fields[1]))
        except ValueError:
            return None
    return pids


def _powerpoint_running() -> bool:
    pids = _powerpoint_pids()
    # Fail closed: an unreadable process list cannot rule out a user
    # session, so spawning a second instance must be refused.
    return True if pids is None else bool(pids)


def _is_real_powerpoint(application) -> bool:
    try:
        return (Path(str(application.Path)) / "POWERPNT.EXE").exists()
    except Exception:
        return False


def _timeout_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be a positive finite seconds value"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite seconds value")
    return value


class _WorkerStatus:
    """Worker-side phase/ownership publisher (atomic temp+replace writes)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._state: dict = {
            "phase": None, "attempt": 0, "owned": [], "protected": [],
        }

    def _flush(self) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        error: OSError | None = None
        for _ in range(10):
            try:
                tmp.write_text(
                    json.dumps(self._state), encoding="utf-8"
                )
                os.replace(tmp, self._path)
                return
            except OSError as exc:
                # Parent may hold the file open for a read on Windows.
                error = exc
                time.sleep(0.02)
        raise error

    def snapshot(self) -> dict:
        """Latest in-memory state — carried in the result document so the
        parent can prefer it over a status file whose last flush failed."""
        return {
            "phase": self._state["phase"],
            "attempt": self._state["attempt"],
            "owned": [dict(r) for r in self._state["owned"]],
            "protected": list(self._state["protected"]),
        }

    def publish(self, kind: str, payload) -> None:
        if kind == "phase":
            self._state["phase"] = payload["phase"]
            self._state["attempt"] = payload["attempt"]
        elif kind == "own":
            self._state["owned"].append(payload)
        elif kind == "protect":
            self._state["protected"].append(payload)
            self._state["owned"] = [
                record
                for record in self._state["owned"]
                if record.get("pid") != payload
            ]
        self._flush()


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _terminate_spawned(proc) -> None:
    """Bounded cleanup of a Popen handle we just created ourselves."""
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass


def _publish_own(publish, proc, exe: Path) -> None:
    """Immediately publish a spawned POWERPNT's verified identity.

    Fails closed: a missing creation_time or a failing publisher raises —
    the caller terminates the just-spawned process and propagates.
    """
    if publish is None or proc is None:
        return
    import psutil

    creation_time = psutil.Process(proc.pid).create_time()
    publish(
        "own",
        {
            "pid": proc.pid,
            "creation_time": creation_time,
            "exe": str(exe.resolve()),
            "parent_pid": os.getpid(),
        },
    )


def _com_powerpoint():
    """Return the ROT-registered PowerPoint object, or None.

    GetActiveObject only, never DispatchEx: WPS hijacks the PowerPoint
    ProgID/CLSID under HKCU on this class of host, so registry-driven
    activation could spawn foreign wpp.exe processes on every poll. The
    ROT is query-only here — no new process is activated; the caller
    filters the bound object by install path and owning pid before
    touching it.
    """
    import win32com.client

    try:
        return win32com.client.GetActiveObject("PowerPoint.Application")
    except Exception:
        return None


def _start_powerpoint(timeout_s: float = 90.0, publish=None):
    """Return ``(app, proc)`` for a dedicated real-PowerPoint instance.

    Returns ``(None, None)`` when unavailable: no POWERPNT.EXE, real
    PowerPoint already running as a user session, or activation kept
    resolving to WPS (which hijacks the PowerPoint ProgID/CLSID under
    HKCU and squats in the Running Object Table) until the deadline.
    Once real POWERPNT.EXE is running, activation resolves to it
    (running servers win over HKCU registrations), so we spawn it and
    poll. WPS may answer while PowerPoint is still starting — that is
    not fatal, keep polling until the deadline. The caller must already
    have called pythoncom.CoInitialize().

    ``publish`` (optional) receives ("own", record) for each spawned
    POWERPNT — pid/psutil creation_time/absolute exe/parent pid — and
    ("protect", pid) when a matched instance turns out to hold user
    presentations and must be left alone. PowerPoint's Application
    object exposes no HWND, so ownership is proven by process-set
    identity instead: the bound object must report the spawned install
    path AND the system-wide POWERPNT.EXE pid set must be exactly the
    spawned pid. A foreign application is never Quit'ed.
    """
    exe = _powerpoint_exe_path()
    if exe is None or _powerpoint_running():
        return None, None
    # /automation keeps UI suppressed; /safe disables add-ins — a loaded
    # third-party add-in (e.g. AiPPT) destroys the ROT-bound Application
    # object mid-Open, surfacing as RPC_E_DISCONNECTED after ~30s.
    spawn_args = [str(exe), "/automation", "/safe"]
    proc = subprocess.Popen(spawn_args)
    try:
        _publish_own(publish, proc, exe)
    except Exception:
        # Never proceed with an unrecorded spawned process.
        _terminate_spawned(proc)
        raise
    deadline = time.monotonic() + timeout_s
    diag = []
    while time.monotonic() < deadline:
        app = None
        err = ""
        try:
            app = _com_powerpoint()
        except Exception as e:  # pragma: no cover - diagnostics
            err = repr(e)[:120]
        if app is not None and _is_real_powerpoint(app):
            live_pids = _powerpoint_pids()
            if (
                live_pids is None
                or proc.poll() is not None
                or live_pids != {proc.pid}
            ):
                # Not our spawned instance (or it already exited, or a
                # concurrent foreign POWERPNT.EXE appeared): never Quit
                # or configure a foreign application; keep polling.
                app = None
            else:
                try:
                    count = app.Presentations.Count
                except Exception:
                    # Cannot verify the instance is empty: fail closed.
                    count = -1
                if count != 0:
                    # Either a user session appeared inside our instance
                    # or the count is unreadable. Protect the matched PID
                    # from owned cleanup and never return the app. A
                    # protection-publish failure must propagate, not be
                    # swallowed into a returned foreign app.
                    if publish is not None:
                        publish("protect", proc.pid)
                    proc = None
                    return None, None
                return app, proc
        # WPS answered (or activation failed) while real PP still starts.
        diag.append(
            f"{time.monotonic() - (deadline - timeout_s):.1f}s "
            f"proc={proc.poll()} app={'other' if app is not None else None} {err}"
        )
        if proc.poll() is not None:
            # POWERPNT.EXE exited early (e.g. recycled by an exiting
            # instance during COM hand-off); respawn once.
            if getattr(proc, "_respawned", False):
                break
            proc = subprocess.Popen(spawn_args)
            proc._respawned = True  # type: ignore[attr-defined]
            try:
                _publish_own(publish, proc, exe)
            except Exception:
                _terminate_spawned(proc)
                raise
        time.sleep(0.5)
    _LOGGER.warning(
        "PowerPoint did not become attachable within %.0fs; trace: %s",
        timeout_s, " | ".join(diag[-8:]),
    )
    try:
        proc.terminate()
    except Exception:
        pass
    return None, None


def _embed_watchdog(proc, done, stalled, timeout_s=None) -> None:
    """Kill the spawned POWERPNT.EXE if the embed section overruns.

    Presentations.Open/SaveAs are synchronous COM calls; an add-in event
    handler or hidden modal can stall them indefinitely. Killing the
    process makes the blocked call return a com_error that the caller
    converts into a retry with a fresh instance.
    """
    if proc is None:
        return
    budget = _EMBED_WATCHDOG_S if timeout_s is None else timeout_s
    if done.wait(budget):
        return
    stalled.set()
    try:
        proc.kill()
    except Exception:
        pass


def _powerpoint_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
    except ImportError:
        return False
    return (
        _powerpoint_exe_path() is not None
        and not _powerpoint_running()
    )


def _pool_font_dir() -> Path | None:
    candidate = Path(__file__).resolve().parents[1] / "fonts"
    return candidate if candidate.is_dir() else None


def _fs_type(path: Path) -> int | None:
    """OS/2 fsType embedding-permission bits; None if unreadable."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 12:
        return None
    (num_tables,) = struct.unpack(">H", data[4:6])
    for index in range(num_tables):
        entry = 12 + index * 16
        if data[entry : entry + 4] == b"OS/2":
            (offset,) = struct.unpack(">I", data[entry + 8 : entry + 12])
            return struct.unpack(">H", data[offset + 8 : offset + 10])[0]
    return None


def _pool_faces() -> dict[str, list[Path]]:
    """family.casefold() -> all font files of that family in the pool."""
    root = _pool_font_dir()
    faces: dict[str, list[Path]] = {}
    if root is None:
        return faces
    from PIL import ImageFont

    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".ttf", ".otf"}:
            continue
        try:
            family, _style = ImageFont.truetype(str(path), 32).getname()
        except OSError:
            continue
        faces.setdefault(family.casefold(), []).append(path)
    return faces


def _installed_font_names() -> set[str]:
    """Family names registered in HKLM/HKCU Fonts (casefolded)."""
    names: set[str] = set()
    if sys.platform != "win32":
        return names
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(
                hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
            )
        except OSError:
            continue
        index = 0
        try:
            while True:
                name, _value, _type = winreg.EnumValue(key, index)
                names.add(name.split(" (")[0].casefold())
                index += 1
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)
    return names


def _install_pool_faces(paths: list[Path]) -> list[Path]:
    """Per-user font install (no admin): copy + HKCU + session GDI resource."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    user_fonts = Path(local) / "Microsoft" / "Windows" / "Fonts"
    installed: list[Path] = []
    try:
        import ctypes
        import winreg
        from PIL import ImageFont

        user_fonts.mkdir(parents=True, exist_ok=True)
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
        )
        try:
            for src in paths:
                dest = user_fonts / src.name
                try:
                    if not dest.exists():
                        shutil.copyfile(src, dest)
                    family, style = ImageFont.truetype(
                        str(dest), 32
                    ).getname()
                    label = family
                    if style.casefold() not in ("regular", "normal"):
                        label = f"{family} {style}"
                    winreg.SetValueEx(
                        key, f"{label} (TrueType)", 0,
                        winreg.REG_SZ, str(dest),
                    )
                    ctypes.windll.gdi32.AddFontResourceW(str(dest))
                    installed.append(dest)
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass
    return installed


def _rewrite_typefaces(
    pptx_path: str | Path, mapping: dict[str, str]
) -> dict[str, str]:
    """Rewrite typeface= attributes in slide XML; return the applied map."""
    path = Path(pptx_path)
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    applied: dict[str, str] = {}
    for name, payload in members.items():
        if not (
            name.endswith(".xml")
            and name.startswith(_XML_FONT_PARTS)
        ):
            continue
        rewritten = payload
        for old, new in mapping.items():
            needle = f'typeface="{old}"'.encode()
            if needle in rewritten:
                rewritten = rewritten.replace(
                    needle, f'typeface="{new}"'.encode()
                )
                applied[old] = new
        if rewritten != payload:
            members[name] = rewritten
    if applied:
        staging = path.with_suffix(".rewrite-tmp.pptx")
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        shutil.move(staging, path)
    return applied


def _resolve_substitutions(missing: list[str]) -> dict[str, str]:
    """usable {core: substitute} map; installs pool faces for COM to see."""
    pool = _pool_faces()
    installed = _installed_font_names()
    mapping: dict[str, str] = {}
    for used in missing:
        substitute = SUBSTITUTES.get(used)
        if not substitute:
            continue
        key = substitute.casefold()
        paths = pool.get(key, [])
        if paths:
            embeddable = [
                path
                for path in paths
                if ((_fs_type(path) or 0) & 0x0002) == 0
            ]
            if _install_pool_faces(embeddable):
                mapping[used] = substitute
            continue
        if key in installed:
            mapping[used] = substitute
    return mapping


def _embed_fonts_in_process(src: str | Path, dst: str | Path, *, publish=None) -> dict:
    """Blocking COM embed — runs inside the supervised worker subprocess."""
    if sys.platform != "win32":
        raise RuntimeError("font embedding requires Windows with PowerPoint")
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise RuntimeError(
            "font embedding requires pywin32 and PowerPoint"
        ) from error

    # Fail fast before touching the filesystem: no usable server means the
    # call cannot succeed regardless of the inputs.
    if _powerpoint_exe_path() is None or _powerpoint_running():
        raise RuntimeError(
            "real Microsoft PowerPoint unavailable: not installed or "
            "already running as a user session"
        )

    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src_usage = collect_font_usage(src_path)
    # Call-time validation: the env override must never crash module
    # import, and an invalid value is rejected before any COM work.
    watchdog_s = _timeout_env(_EMBED_TIMEOUT_ENV, _EMBED_WATCHDOG_S)

    # A writable open is required: with ReadOnly=True the
    # EmbedTrueTypeFonts property cannot be set and SaveAs ignores the
    # embed argument. The source itself is never saved (only SaveAs to
    # dst), so it is left untouched.
    def embed_once(from_path: Path, out_path: Path) -> list:
        # Deliberately never Close the presentation: closing the last
        # file-based presentation revokes this automation instance's
        # COM registration and kills the bound object. Cleanup is left
        # to Quit()/process termination after all rounds finish.
        presentation = application.Presentations.Open(
            str(from_path),
            ReadOnly=False,
            Untitled=False,
            WithWindow=False,
        )
        # presentation.Fonts does not yield items via iteration; index
        # it. WPS's typeinfo-backed dispatch rejects Fonts.Count via
        # InvokeTypes, so wrap it as a dumb (name-only) dispatch. The
        # listing is diagnostic only — embedding itself is done by
        # SaveAs below, so a host that refuses Fonts enumeration must
        # not abort the embed.
        com_fonts = []
        try:
            fonts = _dumb_dispatch(presentation.Fonts)
            for index in range(1, fonts.Count + 1):
                font = _dumb_dispatch(fonts(index))
                com_fonts.append(
                    {
                        "name": str(font.Name),
                        "embeddable": bool(font.Embeddable),
                        "embedded": bool(font.Embedded),
                    }
                )
        except Exception as error:
            _LOGGER.warning(
                "presentation Fonts listing failed: %s", error
            )
        presentation.SaveAs(
            str(out_path), _PP_SAVE_AS_OPENXML, _MSO_TRUE
        )
        return com_fonts

    def mark(phase: str, attempt: int) -> None:
        if publish is not None:
            publish("phase", {"phase": phase, "attempt": attempt})

    mark("activation", 1)  # before CoInitialize / first COM activation
    pythoncom.CoInitialize()
    com_fonts: list = []
    substitutions: dict[str, str] = {}
    app_info = {}
    last_attempt = 1
    try:
        for attempt in range(2):
            last_attempt = attempt + 1
            application = proc = None
            done = threading.Event()
            stalled = threading.Event()
            try:
                # Always start a dedicated instance and always quit it.
                # The ROT binding inside _start_powerpoint is only
                # accepted when the system POWERPNT.EXE pid set is
                # exactly the spawned pid, so a concurrent user session
                # cannot be mistaken for ours.
                mark("activation", attempt + 1)
                application, proc = _start_powerpoint(publish=publish)
                if application is None:
                    raise RuntimeError(
                        "real Microsoft PowerPoint unavailable: not "
                        "installed, already running as a user session, "
                        "or hijacked by WPS"
                    )
                mark("embedding", attempt + 1)  # before any app setter/COM call
                for attr, value in (
                    ("AutomationSecurity",
                     _MSO_AUTOMATION_SECURITY_FORCE_DISABLE),
                    ("DisplayAlerts", _PP_ALERTS_NONE),
                ):
                    try:
                        setattr(application, attr, value)
                    except Exception:
                        pass
                threading.Thread(
                    target=_embed_watchdog,
                    args=(proc, done, stalled, watchdog_s),
                    daemon=True,
                ).start()
                try:
                    app_info = {
                        "name": str(application.Name),
                        "version": str(application.Version),
                        "path": str(application.Path),
                    }
                except Exception:
                    app_info = {}

                # Opened presentations are kept open until Quit: closing
                # the last file-based presentation makes this invisible
                # instance revoke its COM registration, so each pass
                # writes a distinct temp path and the winner is moved to
                # dst only after the app released its file locks.
                pass1 = dst_path.with_suffix(".embed-pass1.pptx")
                pass2 = dst_path.with_suffix(".embed-pass2.pptx")
                com_fonts = embed_once(src_path, pass1)
                dst_usage = collect_font_usage(pass1)
                substitutions = _resolve_substitutions(
                    dst_usage["not_embedded"]
                )
                final_pass = pass1
                if substitutions:
                    # Rewrite a copy of the untouched source (never the
                    # first-pass output, whose embeddedFontLst entries
                    # would be re-labelled).
                    staging = dst_path.with_suffix(".subst-src.pptx")
                    try:
                        shutil.copyfile(src_path, staging)
                        _rewrite_typefaces(staging, substitutions)
                        com_fonts = embed_once(staging, pass2)
                    finally:
                        staging.unlink(missing_ok=True)
                    final_pass = pass2
                try:
                    application.Quit()
                except Exception:
                    pass
                application = None
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    pass
                proc = None
                os.replace(final_pass, dst_path)
                for stray in (pass1, pass2):
                    stray.unlink(missing_ok=True)
                dst_usage = collect_font_usage(dst_path)
                break
            except Exception as error:
                crashed = proc is not None and proc.poll() is not None
                transient_com = type(error).__name__ == "com_error"
                if attempt == 0 and (
                    stalled.is_set() or crashed or transient_com
                ):
                    _LOGGER.warning(
                        "PowerPoint embed %s; retrying with a fresh "
                        "instance",
                        "stalled" if stalled.is_set() else "crashed",
                    )
                    continue
                raise
            finally:
                mark("cleanup", attempt + 1)  # before blocking Quit/terminate
                done.set()
                if application is not None:
                    try:
                        application.Quit()
                    except Exception:
                        pass
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
    finally:
        mark("cleanup", last_attempt)  # before CoUninitialize
        pythoncom.CoUninitialize()

    from pptx import Presentation

    Presentation(str(dst_path))
    dst_usage = collect_font_usage(dst_path)
    return {
        "src_usage": src_usage,
        "dst_usage": dst_usage,
        "com_fonts": com_fonts,
        "substitutions": substitutions,
        "app": app_info,
        "portable": not dst_usage["not_embedded"],
    }


def _embed_worker_command(
    src: Path, dst: Path, status_path: Path, result_path: Path
) -> list[str]:
    """Command launching this same file as the supervised embed worker."""
    return [
        sys.executable, "-u", str(Path(__file__).resolve()),
        "--worker", str(src), str(dst),
        "--status-path", str(status_path),
        "--result-path", str(result_path),
    ]


def _read_worker_status(status_path: Path) -> dict | None:
    try:
        payload = json.loads(status_path.read_bytes())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _kill_owned_process(record: dict, protected: set[int], worker_pid: int) -> None:
    """Kill a worker-owned POWERPNT only when its live identity matches."""
    pid = record.get("pid")
    creation = record.get("creation_time")
    exe = record.get("exe")
    parent = record.get("parent_pid")
    if (
        type(pid) is not int
        or type(creation) not in (int, float)
        or not isinstance(exe, str)
        or type(parent) is not int
        or parent != worker_pid
        or pid in protected
    ):
        return
    import psutil

    try:
        process = psutil.Process(pid)
        if (
            process.ppid() != worker_pid
            or process.create_time() != creation
            or Path(process.exe()).resolve() != Path(exe).resolve()
        ):
            return
    except (psutil.Error, OSError):
        return
    try:
        process.kill()
        process.wait(timeout=2)
    except (psutil.Error, OSError):
        pass


def _final_worker_state(status_path: Path, result_path: Path) -> dict | None:
    """Best-known worker state: last status file overlaid with the result
    document's in-memory ``worker_state`` (whose final status flush may
    have failed). Never raises."""
    try:
        status = _read_worker_status(status_path)
    except Exception:
        status = None
    try:
        payload = json.loads(result_path.read_bytes())
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(
        payload.get("worker_state"), dict
    ):
        status = {**(status or {}), **payload["worker_state"]}
    return status


def _cleanup_worker(worker, status: dict | None) -> None:
    """Kill only verified owned POWERPNTs, then the worker itself."""
    owned: list = []
    protected: set[int] = set()
    if isinstance(status, dict):
        raw_owned = status.get("owned")
        if isinstance(raw_owned, list):
            owned = [r for r in raw_owned if isinstance(r, dict)]
        raw_protected = status.get("protected")
        if isinstance(raw_protected, list):
            protected = {p for p in raw_protected if type(p) is int}
    for record in owned:
        try:
            _kill_owned_process(record, protected, worker.pid)
        except Exception:
            pass
    try:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=2)
    except Exception:
        pass


def _supervise_embed_worker(
    worker,
    status_path: Path,
    result_path: Path,
    *,
    activation_timeout: float,
    embed_timeout: float,
    cleanup_timeout: float,
) -> dict:
    """Bound worker phases; drain pipes; return the validated result."""
    budgets = {
        "activation": activation_timeout,
        "embedding": embed_timeout,
        "cleanup": cleanup_timeout,
    }
    started = time.monotonic()
    overall_deadline = started + 2 * (
        activation_timeout + embed_timeout + cleanup_timeout
    ) + 15.0
    # Startup before the first status is charged to the attempt-1
    # activation budget — a late first status never renews it.
    phase_key: tuple = (1, "activation")
    phase_deadline = started + activation_timeout
    phase_name = "activation"
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    status = None
    while True:
        try:
            out, err = worker.communicate(timeout=0.2)
            stdout_parts.append(out or "")
            stderr_parts.append(err or "")
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        latest = _read_worker_status(status_path)
        if latest is not None:
            status = latest
            new_key = (status.get("attempt"), status.get("phase"))
            # Ownership-only updates carry the same key and never extend
            # the current phase deadline; only genuine worker attempts
            # (1/2) and known phases may open a new budget.
            if (
                type(new_key[0]) is int
                and new_key[0] in (1, 2)
                and isinstance(new_key[1], str)
                and new_key[1] in budgets
                and new_key != phase_key
            ):
                phase_key = new_key
                phase_name = new_key[1]
                phase_deadline = now + budgets[new_key[1]]
        if now >= phase_deadline:
            _cleanup_worker(worker, status)
            raise RuntimeError(
                f"font embed worker {phase_name} phase timed out"
            )
        if now >= overall_deadline:
            _cleanup_worker(worker, status)
            raise RuntimeError(
                "font embed worker exceeded the overall time limit"
            )
    # Process exited: re-read the final status file overlaid with the
    # result document's worker_state (its last status flush may have
    # failed). Owned processes are cleaned on every outcome — success,
    # crash, malformed or missing IPC.
    status = _final_worker_state(status_path, result_path) or status
    payload = None
    try:
        payload = json.loads(result_path.read_bytes())
    except (OSError, ValueError):
        pass
    _cleanup_worker(worker, status)
    if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
        raise RuntimeError(
            f"font embed worker exited without a valid result "
            f"(code {worker.returncode})"
        )
    if payload["ok"] is not True:
        error = payload.get("error")
        raise RuntimeError(
            str(error)
            if isinstance(error, str) and error
            else f"font embed worker failed (code {worker.returncode})"
        )
    if worker.returncode != 0:
        raise RuntimeError(
            f"font embed worker reported success but exited with code "
            f"{worker.returncode}"
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("font embed worker result payload is invalid")
    return result


def embed_fonts(src: str | Path, dst: str | Path) -> dict:
    """Supervised embedding: the blocking COM section runs in a same-file
    worker subprocess so activation/setters/Open/SaveAs/Quit are all
    deadline-bounded and only verified owned POWERPNTs are cleaned up."""
    if sys.platform != "win32":
        raise RuntimeError("font embedding requires Windows with PowerPoint")
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError as error:
        raise RuntimeError(
            "font embedding requires pywin32 and PowerPoint"
        ) from error

    # Fail fast before touching the filesystem: no usable server means the
    # call cannot succeed regardless of the inputs.
    if _powerpoint_exe_path() is None or _powerpoint_running():
        raise RuntimeError(
            "real Microsoft PowerPoint unavailable: not installed or "
            "already running as a user session"
        )

    activation_timeout = _timeout_env(_ACTIVATION_TIMEOUT_ENV, 90.0)
    embed_timeout = _timeout_env(_EMBED_TIMEOUT_ENV, _EMBED_WATCHDOG_S)
    cleanup_timeout = _CLEANUP_TIMEOUT_S

    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="font-embed-") as tmp:
        status_path = Path(tmp) / "status.json"
        result_path = Path(tmp) / "result.json"
        worker = subprocess.Popen(
            _embed_worker_command(src_path, dst_path, status_path, result_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            return _supervise_embed_worker(
                worker,
                status_path,
                result_path,
                activation_timeout=activation_timeout,
                embed_timeout=embed_timeout,
                cleanup_timeout=cleanup_timeout,
            )
        finally:
            # Every exit path — success, timeout, crash, malformed IPC or
            # an unexpected supervision error — cleans verified owned
            # children from the best-known state, then the worker itself.
            _cleanup_worker(
                worker, _final_worker_state(status_path, result_path)
            )


def _run_embed_worker(src: str, dst: str, status_path: str, result_path: str) -> int:
    """Internal worker entry: run the COM embed, publish status/result."""
    status = _WorkerStatus(status_path)
    try:
        result = _embed_fonts_in_process(src, dst, publish=status.publish)
    except BaseException as error:
        _write_json(
            Path(result_path),
            {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "worker_state": status.snapshot(),
            },
        )
        return 1
    _write_json(
        Path(result_path),
        {
            "ok": True,
            "result": result,
            "worker_state": status.snapshot(),
        },
    )
    return 0


def embed_fonts_enabled() -> bool:
    raw = os.environ.get(EMBED_FONTS_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _default_report_path(pptx_path: Path) -> Path:
    return pptx_path.with_suffix(".embed-report.json")


def _write_report(report_path: Path | None, payload: dict) -> None:
    if report_path is None:
        return
    try:
        report_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as error:
        _LOGGER.warning("font embed report write failed: %s", error)


def embed_pptx_in_place(
    pptx_path: str | Path,
    report_path: str | Path | None = None,
) -> dict:
    """Embed fonts into ``pptx_path`` in place; never raises.

    Delivery calls this for every produced PPTX. Failures degrade to a
    skipped report and keep the original file untouched.
    """
    path = Path(pptx_path)
    report = Path(report_path) if report_path is not None else _default_report_path(path)
    if not embed_fonts_enabled():
        payload = {"embedded": False, "skipped": True, "reason": f"{EMBED_FONTS_ENV} disabled"}
        _write_report(report, payload)
        return payload
    try:
        usage = collect_font_usage(path)
        if usage["not_embedded"] == [] and usage["embedded"]:
            payload = {
                "embedded": True,
                "already": True,
                "dst_usage": usage,
                "portable": True,
            }
            _write_report(report, payload)
            return payload
        tmp = path.with_name(f".{path.stem}.embed-tmp.pptx")
        try:
            result = embed_fonts(path, tmp)
        except Exception as error:
            tmp.unlink(missing_ok=True)
            raise error
        os.replace(tmp, path)
        payload = {"embedded": True, **result}
        _write_report(report, payload)
        return payload
    except Exception as error:
        _LOGGER.warning("font embedding skipped for %s: %s", path, error)
        payload = {"embedded": False, "skipped": True, "reason": str(error)}
        _write_report(report, payload)
        return payload


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":
        # Internal same-file worker mode; not a public CLI surface.
        parser = argparse.ArgumentParser()
        parser.add_argument("--worker", action="store_true")
        parser.add_argument("src")
        parser.add_argument("dst")
        parser.add_argument("--status-path", required=True)
        parser.add_argument("--result-path", required=True)
        args = parser.parse_args(argv)
        raise SystemExit(
            _run_embed_worker(
                args.src, args.dst, args.status_path, args.result_path
            )
        )
    parser = argparse.ArgumentParser(
        description="Embed used fonts into a PPTX via PowerPoint."
    )
    parser.add_argument("src")
    parser.add_argument("dst")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    result = embed_fonts(args.src, args.dst)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.report:
        Path(args.report).write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
