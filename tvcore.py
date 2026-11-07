"""Core logic for TV Cleaner: ADB access, device probing, list matching, profiles.

The heavy lifting (disable, install, theme, restore) is done by the cleanyourtv repo's own scripts,
so their safety checks (core packages, Home guard, setupwraith Wi-Fi check, recovery record) stay in
force. This module only finds the device, proposes a list and remembers what it changed.
"""
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.json"

# Portable build: everything the tools keep in the user's home (the undo records in .cleanyourtv, our
# profiles in .tvcleaner, adb's own key in .android) lives in <root>\data instead, and adb runs its own
# server on another port, so the folder works the same from any PC and leaves no traces behind.
PORTABLE_ROOT = HERE.parent if (HERE.parent / "portable.txt").exists() else None
if PORTABLE_ROOT:
    _data = PORTABLE_ROOT / "data"
    _data.mkdir(exist_ok=True)
    os.environ["USERPROFILE"] = str(_data)
    os.environ["HOME"] = str(_data)
    os.environ["ANDROID_USER_HOME"] = str(_data / ".android")
    os.environ["ANDROID_ADB_SERVER_PORT"] = "5038"

PROFILES = Path.home() / ".tvcleaner" / "devices"

LAUNCHER_PKG = "com.gathin.tvhome"
CLOCK_DREAM = "com.gathin.tvhome/.ClockDream"
SETUPWRAITH = "com.google.android.tungsten.setupwraith"
KNOWN_STOCK = ["com.google.android.tvlauncher", "com.google.android.apps.tv.launcherx",
               "com.google.android.leanbacklauncher", "com.mitv.tvhome"]

# Same guard as tools/disable.ps1: without these Android may not boot (#884, #1578).
CORE_RE = re.compile(
    r"^(android|android\.ext\.(services|shared)|com\.android\.sdksandbox|com\.android\.location\.fused|"
    r"com\.android\.systemui|com\.android\.shell|com\.android\.phone|com\.android\.settings|"
    r"com\.android\.tv\.settings|com\.(google\.)?android\.(permissioncontroller|packageinstaller|"
    r"ext\.services|ext\.shared|modulemetadata)|com\.google\.android\.overlay\.modules.*|"
    r"com\.android\.providers\..*|com\.cltv\.mal|com\.mediatek\.(tis|tv\.service|tv\.agent))$")
CORE_OK = re.compile(r"^com\.android\.providers\.(calendar|contacts|userdictionary|blockednumber)$")
# What the TV needs to work (PROMPT.md): never proposed from other TVs' lists.
KEEP_RE = re.compile(
    r"(^com\.android\.vending$|^com\.google\.android\.(gms|gsf)|inputmethod|keyboard|blerc|"
    r"keyhandler|globalkey|remote|bluetooth|hdmi|cec|tvinput|inputsource|dtvkit|tv\.settings|tvsetup|"
    r"wifi|networkstack|audio|sound|dolby|factory|fac\.|hkcfac|^com\.droidlogic(?!\.updater))", re.I)
# Things the owner decides on (PROMPT.md checklist question).
CHOICE = [
    (re.compile(r"katniss|assistant|voice", re.I), "Голосовой ассистент"),
    (re.compile(r"mediashell|chromecast", re.I), "Chromecast"),
    (re.compile(r"update|upgrade|\.ota|fota", re.I), "Обновления прошивки"),
    (re.compile(r"smartshare|miracast|wfd", re.I), "Miracast / трансляция экрана"),
    (re.compile(r"netflix|amazonvideo|youtube|disney|hotstar|hbo|spotify|play\.games|videos|"
                r"music|primevideo|apple\.atve|plex|kodi|twitch", re.I), "Стриминги и приложения"),
]
PKG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
# For a TV with no tested list: only these well-known kinds of package are proposed from other TVs'
# lists, and only the clearly harmless ones are ticked by default.
SUGGEST_RE = re.compile(
    r"(feedback|partnersetup|adservices|ondevicepersonalization|federatedcompute|analytics|statistic|"
    r"telemetry|tracking|bugreport|autoinstalls\.config|tvrecommendations|partnercustomizer|"
    r"play\.games|marvin\.talkback|printspooler|syncadapters\.calendar|healthconnect|health\.connect|"
    r"backdrop|dreamx|youtube|netflix|amazonvideo|hotstar|disney|tvmusic|google\.android\.videos|"
    r"katniss|mediashell|updater|systemupdate|updateservice|utsmta)", re.I)
SAFE_DEFAULT_RE = re.compile(
    r"(feedback|partnersetup|adservices|ondevicepersonalization|federatedcompute|analytics|statistic|"
    r"telemetry|tracking|bugreport|autoinstalls\.config|play\.games|marvin\.talkback|printspooler|"
    r"syncadapters\.calendar|healthconnect|health\.connect)", re.I)


def is_core(pkg):
    return bool(CORE_RE.match(pkg)) and not CORE_OK.match(pkg)


def choice_group(pkg):
    for rx, name in CHOICE:
        if rx.search(pkg):
            return name
    return None


# ---------------------------------------------------------------- settings / paths

def load_settings():
    s = {}
    if SETTINGS_FILE.exists():
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            s = {}
    repo = Path(s.get("repo") or (HERE.parent / "cleanyourtv"))
    adb = s.get("adb") or str(HERE.parent / "platform-tools" / "adb.exe")
    if not Path(adb).exists():
        adb = shutil.which("adb") or adb
    sdk = s.get("android_home") or os.environ.get("ANDROID_HOME") or (
        "C:\\AndroidSDK" if Path("C:/AndroidSDK").exists() else "")
    return {"repo": str(repo), "adb": adb, "android_home": sdk}


def check_environment(cfg):
    """Return a list of human-readable problems; empty when everything needed is there."""
    problems = []
    repo = Path(cfg["repo"])
    if not (repo / "tools" / "disable.ps1").exists():
        problems.append(f"Не найден репозиторий cleanyourtv: {repo}")
    if not (repo / "tools" / "venv" / "Scripts" / "python.exe").exists():
        problems.append("Нет tools\\venv в репозитории: python -m venv tools\\venv; "
                        "tools\\venv\\Scripts\\python.exe -m pip install -r tools\\requirements.txt")
    if not Path(cfg["adb"]).exists():
        problems.append(f"Не найден adb: {cfg['adb']}")
    apk = repo / "launcher" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    if not apk.exists() and not cfg["android_home"]:
        problems.append("Лаунчер ещё не собран и не найден Android SDK (нужны JDK 17 и SDK 34)")
    return problems


def apk_path(cfg):
    return Path(cfg["repo"]) / "launcher" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"


def decode(b):
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


# ---------------------------------------------------------------- adb

class Adb:
    def __init__(self, binary):
        self.binary = binary

    def run(self, *args, timeout=30):
        try:
            p = subprocess.run([self.binary, *args], capture_output=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return p.returncode, (decode(p.stdout) + decode(p.stderr)).replace("\r", "")

    def shell(self, host, cmd, timeout=30):
        rc, out = self.run("-s", host, "shell", cmd, timeout=timeout)
        return out.strip() if rc == 0 else ""

    def devices(self):
        _, out = self.run("devices")
        res = {}
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) == 2:
                res[parts[0]] = parts[1]
        return res

    def mdns(self):
        """[(name, kind, 'ip:port')] for ADB services advertised on the network."""
        _, out = self.run("mdns", "services", timeout=15)
        res = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1].startswith("_adb"):
                res.append((parts[0], parts[1].strip("."), parts[2]))
        return res

    def connect(self, addr):
        rc, out = self.run("connect", addr, timeout=20)
        ok = "connected to" in out and "cannot" not in out
        if ok:
            for _ in range(10):  # "connected" can come before the transport is authorised
                state = self.devices().get(addr)
                if state == "device":
                    return True, out
                time.sleep(1)
            return False, out + f"\nСостояние: {self.devices().get(addr)} (подтвердите отладку на экране ТВ)"
        return False, out

    def pair(self, addr, code):
        rc, out = self.run("pair", addr, code, timeout=30)
        return "Successfully paired" in out, out


# ---------------------------------------------------------------- device probe

def probe(adb, host):
    props = {}
    for key in ("ro.product.model", "ro.product.device", "ro.product.manufacturer", "ro.product.brand",
                "ro.build.version.release", "ro.build.version.sdk", "ro.build.fingerprint",
                "ro.serialno", "ro.boot.serialno"):
        props[key] = adb.shell(host, f"getprop {key}")
    info = {
        "host": host,
        "model": props["ro.product.model"],
        "device": props["ro.product.device"],
        "manufacturer": props["ro.product.manufacturer"],
        "brand": props["ro.product.brand"],
        "android": props["ro.build.version.release"],
        "sdk": int(props["ro.build.version.sdk"] or 0),
        "fingerprint": props["ro.build.fingerprint"],
        "serial": props["ro.serialno"] or props["ro.boot.serialno"] or "",
    }
    pk = lambda out: {l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")}
    info["packages"] = pk(adb.shell(host, "pm list packages -u", timeout=60))
    info["enabled"] = pk(adb.shell(host, "pm list packages -e", timeout=60))
    info["disabled"] = pk(adb.shell(host, "pm list packages -d", timeout=60))
    info["third_party"] = pk(adb.shell(host, "pm list packages -3", timeout=60))
    home = adb.shell(host, "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                           "-c android.intent.category.HOME").splitlines()
    info["home"] = home[-1].strip() if home else ""
    claim = adb.shell(host, "cmd package query-activities --brief -a android.intent.action.MAIN "
                            "-c android.intent.category.HOME")
    info["home_claimants"] = [l.strip() for l in claim.splitlines() if "/" in l]
    lb = adb.shell(host, "cmd package query-activities --brief -a android.intent.action.MAIN "
                         "-c android.intent.category.LEANBACK_LAUNCHER", timeout=60)
    info["tv_apps"] = sorted({l.strip().split("/")[0] for l in lb.splitlines() if "/" in l})
    wifi = adb.shell(host, "dumpsys wifi | grep -o 'cname=[^ ]*' | sort -u", timeout=60)
    info["wifi_owners"] = [w.replace("cname=", "") for w in wifi.split()]
    info["screensaver"] = adb.shell(host, "settings get secure screensaver_components")
    info["accessibility"] = adb.shell(host, "settings get secure enabled_accessibility_services")
    info["accessibility_enabled"] = adb.shell(host, "settings get secure accessibility_enabled")
    info["ota_flag"] = adb.shell(host, "settings get global ota_disable_automatic_update")
    stock = info["home"].split("/")[0]
    info["stock_launcher"] = stock if stock in KNOWN_STOCK else next(
        (p for p in KNOWN_STOCK if any(c.startswith(p + "/") for c in info["home_claimants"])), "")
    info["google_tv"] = "com.google.android.apps.tv.launcherx" in info["packages"]
    info["setupwraith_claims_home"] = any(c.startswith(SETUPWRAITH + "/") for c in info["home_claimants"])
    info["setupwraith_owns_wifi"] = any("setupwraith" in w for w in info["wifi_owners"])
    return info


def profile_key(info):
    base = info.get("serial") or f"{info['model']}-{info['host'].split(':')[0]}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)


# ---------------------------------------------------------------- lists from the repo

def parse_list(path):
    """[(pkg, section, comment, active)] from a debloat list; section is the last '# ...' heading."""
    rows, section, started = [], "", False
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            first = body.split()[0] if body else ""
            if PKG_RE.match(first):
                # A commented-out package after the header: kept on purpose by the list's author.
                # Package names inside the header notes are explanations, not entries.
                if started:
                    rows.append((first, section, body[len(first):].strip(" #"), False))
                continue
            if body and len(body) < 90 and not body.startswith(("-", "Apply", "Undo", "Fingerprint")):
                section = body
            continue
        pkg, _, comment = line.partition("#")
        pkg = pkg.split()[0] if pkg.split() else ""
        if PKG_RE.match(pkg):
            started = True
            rows.append((pkg, section, comment.strip(), True))
    return rows


class Library:
    def __init__(self, repo):
        self.repo = Path(repo)
        self.lists = []  # dicts: path, folder, title, text
        self.freq = Counter()
        ex = self.repo / "examples"
        for folder in sorted(ex.iterdir()) if ex.exists() else []:
            if not folder.is_dir():
                continue
            title = ""
            tvj = folder / "tv.json"
            if tvj.exists():
                try:
                    title = json.loads(tvj.read_text(encoding="utf-8")).get("tv", "")
                except ValueError:
                    pass
            for lst in sorted(folder.glob("debloat*.txt")):
                text = lst.read_text(encoding="utf-8", errors="replace")
                head = "\n".join(l for l in text.splitlines()[:12] if l.startswith("#"))
                self.lists.append({"path": lst, "folder": folder.name, "title": title or folder.name,
                                   "head": head})
                if lst.name == "debloat.txt":
                    for pkg, _, _, active in parse_list(lst):
                        if active:
                            self.freq[pkg] += 1

    def match(self, info):
        """Lists tested on this exact model first, then lists for the same board (device codename)."""
        model = info["model"].lower()
        device = info["device"].lower()
        exact, similar = [], []
        for l in self.lists:
            hay = (l["title"] + "\n" + l["head"]).lower()
            if model and re.search(r"(?<![a-z0-9])" + re.escape(model) + r"(?![a-z0-9])", hay):
                exact.append(l)
            elif device and len(device) >= 3 and re.search(
                    r"(?<![a-z0-9])" + re.escape(device) + r"(?![a-z0-9])", hay):
                similar.append(l)
        return exact, similar


def build_plan(info, lib, chosen_list=None, similar=()):
    """Rows for the checklist: dict(pkg, group, note, checked, source)."""
    present = info["packages"]
    rows, seen = [], set()

    def add(pkg, group, note, checked, source):
        if pkg in seen or pkg not in present or is_core(pkg) or pkg == LAUNCHER_PKG:
            return
        if pkg in info.get("third_party", ()):
            return  # apps the owner installed are never part of a debloat
        if pkg in KNOWN_STOCK:
            return  # the stock launcher is handed over by build-and-install, never by the list
        seen.add(pkg)
        cg = choice_group(pkg)
        rows.append({"pkg": pkg, "group": cg or group or "Прочее", "note": note,
                     "checked": checked, "source": source, "choice": bool(cg)})

    if info["setupwraith_claims_home"]:
        add(SETUPWRAITH, "Мастер настройки Google TV (перехватывает Home)",
            "отключается первым; заново спарить пульт — включить обратно", True, "google-tv")

    if chosen_list:
        for pkg, section, comment, active in parse_list(chosen_list):
            add(pkg, section, comment, active, "список")
    else:
        sources = list(similar)
        for l in sources:
            for pkg, section, comment, active in parse_list(l["path"]):
                if active and not KEEP_RE.search(pkg):
                    add(pkg, section, comment + f" (из {l['folder']})", True, "похожее устройство")
        for pkg, n in lib.freq.most_common():
            if n < 3:
                break
            if SUGGEST_RE.search(pkg) and not KEEP_RE.search(pkg):
                add(pkg, "Часто отключают на других ТВ", f"отключён в {n} списках",
                    bool(SAFE_DEFAULT_RE.search(pkg)), "статистика")
        # Vendor packages no list mentions but whose name says what they are.
        for pkg in sorted(present):
            if SUGGEST_RE.search(pkg) and not KEEP_RE.search(pkg) and not pkg.startswith("com.android."):
                add(pkg, "Похоже на телеметрию/обновления производителя", "по названию, проверьте",
                    bool(SAFE_DEFAULT_RE.search(pkg)), "по названию")
    return rows


def write_list(path, info, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [f"# {info['manufacturer']} {info['model']} ({info['device']}), Android {info['android']} "
            f"(SDK {info['sdk']})",
            f"# Fingerprint: {info['fingerprint']}",
            "# Written by TV Cleaner. Apply/undo with the cleanyourtv tools; packages are disabled, not removed.",
            ""]
    body, last = [], None
    ordered = sorted(rows, key=lambda r: r["pkg"] != SETUPWRAITH)  # setupwraith first (Home guard)
    for r in ordered:
        if r["group"] != last:
            body += ["", f"# {r['group']}"]
            last = r["group"]
        body.append(r["pkg"] + (f"   # {r['note']}" if r["note"] else ""))
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- profiles

def profile_dir(key):
    return PROFILES / key


def save_profile(key, data):
    d = profile_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "device.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_profiles():
    res = []
    if PROFILES.exists():
        for d in sorted(PROFILES.iterdir()):
            f = d / "device.json"
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    data["_key"] = d.name
                    res.append(data)
                except ValueError:
                    pass
    return res


def backup(adb, host, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name, cmd in [("packages-all.txt", "pm list packages -u"), ("packages-disabled.txt", "pm list packages -d"),
                      ("getprop.txt", "getprop"), ("settings-global.txt", "settings list global"),
                      ("settings-secure.txt", "settings list secure"), ("settings-system.txt", "settings list system"),
                      ("home.txt", "cmd package query-activities --brief -a android.intent.action.MAIN "
                                   "-c android.intent.category.HOME")]:
        (dest / name).write_text(adb.shell(host, cmd, timeout=90), encoding="utf-8")
    return dest


def ps_script(cfg, script, *args):
    """Command line for one of the repo's PowerShell scripts."""
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(Path(cfg["repo"]) / script), *map(str, args)]


def script_env(cfg, skip_build=True):
    env = dict(os.environ)
    env["TV_ADB_BINARY"] = cfg["adb"]
    env["PYTHONIOENCODING"] = "utf-8"
    if cfg["android_home"]:
        env["ANDROID_HOME"] = cfg["android_home"]
    if skip_build:
        env["SKIP_BUILD"] = "1"
    else:
        env.pop("SKIP_BUILD", None)
    for k in ("FORCE_HOME", "FORCE_SETUPWRAITH", "TVINPUT_ALLOW", "HANDLERS_ALLOW"):
        env.pop(k, None)
    return env
