"""Build the portable TV Cleaner folder and zip.

    py -3.14 make_portable.py

Layout of dist\\TVCleaner:
    TV Cleaner.bat            start here
    portable.txt              marker: keep all data inside this folder
    app\\                      tvcleaner.py, tvcore.py
    platform-tools\\           adb
    cleanyourtv\\              the repo's tools, lists and the built launcher APK
        tools\\venv\\Scripts\\    a private Python with tkinter and adb-shell (the path the repo scripts expect)
    data\\                     undo records, profiles and adb key (created on first run)
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent / "cleanyourtv"
PTOOLS = HERE.parent / "platform-tools"
PYHOME = Path(sys.base_prefix)
DIST = HERE / "dist" / "TVCleaner"

LIB_SKIP = {"site-packages", "test", "idlelib", "ensurepip", "lib2to3", "pydoc_data", "turtledemo",
            "__pycache__", "tkinter/test", "unittest/test"}


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def main():
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # app
    for f in ("tvcleaner.py", "tvcore.py"):
        copy(HERE / f, DIST / "app" / f)
    copy(HERE / "PORTABLE-README.md", DIST / "README.md")
    (DIST / "portable.txt").write_text("TV Cleaner portable: data is kept in .\\data\n", encoding="utf-8")
    (DIST / "TV Cleaner.bat").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0cleanyourtv\\tools\\venv\\Scripts\\pythonw.exe" '
        '"%~dp0app\\tvcleaner.py"\r\n', encoding="ascii")
    (DIST / "TV Cleaner (console).bat").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\n"%~dp0cleanyourtv\\tools\\venv\\Scripts\\python.exe" '
        '"%~dp0app\\tvcleaner.py"\r\npause\r\n', encoding="ascii")

    # adb
    for f in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll", "NOTICE.txt",
              "source.properties"):
        if (PTOOLS / f).exists():
            copy(PTOOLS / f, DIST / "platform-tools" / f)

    # repo subset: tools (no venv, no adb key), lists, the launcher installer and its APK
    r = DIST / "cleanyourtv"
    copy(REPO / "LICENSE", r / "LICENSE")
    for f in (REPO / "tools").iterdir():
        if f.is_file() and f.suffix in (".py", ".ps1", ".txt", ".md"):
            copy(f, r / "tools" / f.name)
    shutil.copytree(REPO / "examples", r / "examples",
                    ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg"))
    copy(REPO / "launcher" / "build-and-install.ps1", r / "launcher" / "build-and-install.ps1")
    rel = REPO / "launcher" / "app" / "build" / "outputs" / "apk" / "release"
    for f in ("app-release.apk", "output-metadata.json"):
        copy(rel / f, r / "launcher" / "app" / "build" / "outputs" / "apk" / "release" / f)
    for f in ("FONT-LICENSE-OFL.txt", "ICONS-LICENSE-FLUENT-EMOJI.txt"):
        if (REPO / "launcher" / f).exists():
            copy(REPO / "launcher" / f, r / "launcher" / f)

    # private Python at the path the repo's scripts call: tools\venv\Scripts\python.exe
    py = r / "tools" / "venv" / "Scripts"
    for f in PYHOME.glob("*.dll"):
        copy(f, py / f.name)
    for f in ("python.exe", "pythonw.exe", "LICENSE.txt"):
        copy(PYHOME / f, py / f)
    shutil.copytree(PYHOME / "DLLs", py / "DLLs", ignore=shutil.ignore_patterns("_test*", "*.lib"))
    shutil.copytree(PYHOME / "tcl", py / "tcl", ignore=shutil.ignore_patterns("*.lib", "*.sh", "nmake"))

    def lib_ignore(d, names):
        rel_d = Path(d).relative_to(PYHOME / "Lib").as_posix()
        return [n for n in names if n in LIB_SKIP or f"{rel_d}/{n}".lstrip("./") in LIB_SKIP
                or n.endswith((".pyc",))]
    shutil.copytree(PYHOME / "Lib", py / "Lib", ignore=lib_ignore)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
                    "--target", str(py / "Lib" / "site-packages"), "-r", str(REPO / "tools" / "requirements.txt")],
                   check=True)

    # never ship keys or local data
    for bad in list(DIST.rglob("adbkey*")) + list(DIST.rglob("__pycache__")):
        shutil.rmtree(bad) if bad.is_dir() else bad.unlink()

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"folder: {DIST}  ({size / 1e6:.0f} MB)")
    z = shutil.make_archive(str(HERE / "dist" / "TVCleaner-portable"), "zip", DIST.parent, DIST.name)
    print(f"zip: {z}  ({Path(z).stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
