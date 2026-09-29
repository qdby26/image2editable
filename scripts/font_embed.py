"""Embed used typefaces into a PPTX via PowerPoint COM (Windows only)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import struct
import sys
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
_EMBED_WATCHDOG_S = float(
    os.environ.get("IMAGE2EDITABLE_FONT_EMBED_TIMEOUT", "240")
)
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


def _powerpoint_running() -> bool:
    import subprocess

    try:
        listing = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return False
    return "POWERPNT.EXE" in listing.upper()


def _is_real_powerpoint(application) -> bool:
    try:
        return (Path(str(application.Path)) / "POWERPNT.EXE").exists()
    except Exception:
        return False


def _com_powerpoint():
    """Return the activated PowerPoint object (real PP or hijacked WPS)."""
    import win32com.client
    for progid in (_POWERPOINT_CLSID, "PowerPoint.Application"):
        try:
            return win32com.client.DispatchEx(progid)
        except Exception:
            continue
    return None


def _start_powerpoint(timeout_s: float = 90.0):
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
    """
    import subprocess
    import time

    exe = _powerpoint_exe_path()
    if exe is None or _powerpoint_running():
        return None, None
    proc = subprocess.Popen([str(exe), "/automation"])
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
            try:
                if app.Presentations.Count > 0:
                    # A user session appeared between our checks; leave it.
                    proc = None
                    return None, None
            except Exception:
                pass
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
            proc = subprocess.Popen([str(exe), "/automation"])
            proc._respawned = True  # type: ignore[attr-defined]
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


def _embed_watchdog(proc, done, stalled) -> None:
    """Kill the spawned POWERPNT.EXE if the embed section overruns.

    Presentations.Open/SaveAs are synchronous COM calls; an add-in event
    handler or hidden modal can stall them indefinitely. Killing the
    process makes the blocked call return a com_error that the caller
    converts into a retry with a fresh instance.
    """
    if proc is None:
        return
    if done.wait(_EMBED_WATCHDOG_S):
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


def embed_fonts(src: str | Path, dst: str | Path) -> dict:
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

    # A writable open is required: with ReadOnly=True the
    # EmbedTrueTypeFonts property cannot be set and SaveAs ignores the
    # embed argument. The source itself is never saved (only SaveAs to
    # dst), so it is left untouched.
    def embed_once(from_path: Path) -> list:
        presentation = application.Presentations.Open(
            str(from_path),
            ReadOnly=False,
            Untitled=False,
            WithWindow=False,
        )
        try:
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
                str(dst_path), _PP_SAVE_AS_OPENXML, _MSO_TRUE
            )
            return com_fonts
        finally:
            try:
                presentation.Close()
            except Exception:
                pass

    import threading

    pythoncom.CoInitialize()
    com_fonts: list = []
    substitutions: dict[str, str] = {}
    app_info = {}
    try:
        for attempt in range(2):
            application = proc = None
            done = threading.Event()
            stalled = threading.Event()
            try:
                # Always start a dedicated instance and always quit it:
                # attaching to an already-running presentation app via
                # GetActiveObject risks touching user sessions and
                # produces inconsistent dispatch state.
                application, proc = _start_powerpoint()
                if application is None:
                    raise RuntimeError(
                        "real Microsoft PowerPoint unavailable: not "
                        "installed, already running as a user session, "
                        "or hijacked by WPS"
                    )
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
                    args=(proc, done, stalled),
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

                com_fonts = embed_once(src_path)
                dst_usage = collect_font_usage(dst_path)
                substitutions = _resolve_substitutions(
                    dst_usage["not_embedded"]
                )
                if substitutions:
                    # Rewrite a copy of the untouched source (never the
                    # first-pass output, whose embeddedFontLst entries
                    # would be re-labelled).
                    staging = dst_path.with_suffix(".subst-src.pptx")
                    try:
                        shutil.copyfile(src_path, staging)
                        _rewrite_typefaces(staging, substitutions)
                        com_fonts = embed_once(staging)
                    finally:
                        staging.unlink(missing_ok=True)
                    dst_usage = collect_font_usage(dst_path)
                break
            except Exception:
                crashed = proc is not None and proc.poll() is not None
                if attempt == 0 and (stalled.is_set() or crashed):
                    _LOGGER.warning(
                        "PowerPoint embed %s; retrying with a fresh "
                        "instance",
                        "stalled" if stalled.is_set() else "crashed",
                    )
                    continue
                raise
            finally:
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
