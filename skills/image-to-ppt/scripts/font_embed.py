"""Embed used typefaces into a PPTX via PowerPoint COM (Windows only)."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_PP_SAVE_AS_OPENXML = 24
_MSO_TRUE = -1


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


def _powerpoint_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
        from win32com.client import DispatchEx  # noqa: F401
        import win32com.client
        win32com.client.GetActiveObject("PowerPoint.Application")
        return True
    except Exception:
        try:
            from win32com.client import DispatchEx

            app = DispatchEx("PowerPoint.Application")
            app.Quit()
            return True
        except Exception:
            return False


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

    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src_usage = collect_font_usage(src_path)

    pythoncom.CoInitialize()
    application = None
    presentation = None
    started = False
    try:
        # Always start a dedicated instance and always quit it: attaching
        # to an already-running presentation app via GetActiveObject risks
        # touching user sessions and produces inconsistent dispatch state.
        from win32com.client import DispatchEx

        application = DispatchEx("PowerPoint.Application")
        started = True
        try:
            app_info = {
                "name": str(application.Name),
                "version": str(application.Version),
                "path": str(application.Path),
            }
        except Exception:
            app_info = {}

        # A writable open is required: with ReadOnly=True the
        # EmbedTrueTypeFonts property cannot be set and SaveAs ignores the
        # embed argument. The source itself is never saved (only SaveAs to
        # dst), so it is left untouched.
        presentation = application.Presentations.Open(
            str(src_path),
            ReadOnly=False,
            Untitled=False,
            WithWindow=False,
        )
        # presentation.Fonts does not yield items via iteration; index it.
        # WPS's typeinfo-backed dispatch rejects Fonts.Count via InvokeTypes,
        # so wrap it as a dumb (name-only) dispatch.
        fonts = _dumb_dispatch(presentation.Fonts)
        com_fonts = []
        for index in range(1, fonts.Count + 1):
            font = _dumb_dispatch(fonts(index))
            com_fonts.append(
                {
                    "name": str(font.Name),
                    "embeddable": bool(font.Embeddable),
                    "embedded": bool(font.Embedded),
                }
            )
        presentation.SaveAs(
            str(dst_path), _PP_SAVE_AS_OPENXML, _MSO_TRUE
        )
    finally:
        if presentation is not None:
            presentation.Close()
        if application is not None and started:
            application.Quit()
        pythoncom.CoUninitialize()

    from pptx import Presentation

    Presentation(str(dst_path))
    dst_usage = collect_font_usage(dst_path)
    return {
        "src_usage": src_usage,
        "dst_usage": dst_usage,
        "com_fonts": com_fonts,
        "app": app_info,
        "portable": not dst_usage["not_embedded"],
    }


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
