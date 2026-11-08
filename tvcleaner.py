"""TV Cleaner: a window wizard around the cleanyourtv tools.

Connect -> choose what to disable -> launcher options -> run -> (reboot and check). Undo on its own tab.
Run with:  python tvcleaner.py
"""
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import tvcore as core

CHECK, UNCHECK = "☑", "☐"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TV Cleaner")
        self.geometry("1000x720")
        self.minsize(860, 600)
        self.cfg = core.load_settings()
        self.adb = core.Adb(self.cfg["adb"])
        self.lib = None
        self.info = None
        self.rows = []
        self.busy = False
        self.q = queue.Queue()

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=8)
        style.configure("H.TLabel", font=("Segoe UI", 12, "bold"))

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=4)
        logf = ttk.Frame(paned)
        paned.add(logf, weight=1)
        self.log_text = tk.Text(logf, height=9, wrap="word", font=("Consolas", 9), state="disabled",
                                background="#111", foreground="#ddd")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Готово")
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken").pack(fill="x", side="bottom")

        self._tab_connect()
        self._tab_plan()
        self._tab_launcher()
        self._tab_run()
        self._tab_restore()
        for i in (1, 2, 3):
            self.nb.tab(i, state="disabled")

        self.after(100, self._pump)
        problems = core.check_environment(self.cfg)
        if problems:
            messagebox.showwarning("Не всё готово", "\n\n".join(problems))
        self.task(self._load_library, "Загружаю списки репозитория…")

    # ------------------------------------------------------------ plumbing
    def log(self, text):
        self.q.put(("log", text))

    def ui(self, fn, *a, **kw):
        self.q.put(("call", lambda: fn(*a, **kw)))

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", val.rstrip("\n") + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                else:
                    val()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def task(self, fn, status, *args):
        if self.busy:
            messagebox.showinfo("Подождите", "Предыдущая операция ещё выполняется.")
            return
        self.busy = True
        self.status.set(status)

        def run():
            try:
                fn(*args)
            except Exception as e:  # show any failure instead of dying silently
                self.log(f"ОШИБКА: {e}")
                self.ui(messagebox.showerror, "Ошибка", str(e))
            finally:
                self.busy = False
                self.ui(self.status.set, "Готово")
        threading.Thread(target=run, daemon=True).start()

    def stream(self, cmd, env=None, cwd=None):
        """Run a command, copy its output into the log line by line, return (rc, full text)."""
        self.log("$ " + " ".join(Path(c).name if i < 6 and "\\" in c else c for i, c in enumerate(cmd)))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             cwd=cwd or self.cfg["repo"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = []
        for raw in iter(p.stdout.readline, b""):
            line = core.decode(raw).rstrip("\r\n")
            out.append(line)
            if line.strip().startswith("at ") or line.startswith("chunk "):
                continue  # Java stack frames and upload progress are noise here
            self.log(line)
        p.wait()
        return p.returncode, "\n".join(out)

    def _load_library(self):
        self.lib = core.Library(self.cfg["repo"])
        self.log(f"Списков в репозитории: {len(self.lib.lists)}")
        self.ui(self._refresh_profiles)

    # ------------------------------------------------------------ tab 1: connect
    def _tab_connect(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text=" 1. Подключение ")
        ttk.Label(f, text="Подключите приставку или телевизор", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, wraplength=900, justify="left", text=(
            "На ТВ: Настройки → Об устройстве → 7 раз нажать «Сборка», затем «Для разработчиков» → включить "
            "«Отладку по USB» и «Отладку по сети» или «Отладку по Wi-Fi». Компьютер и ТВ должны быть в одной сети."
        )).pack(anchor="w", pady=(2, 10))

        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="Найти устройства в сети", command=self.on_scan).pack(side="left")
        ttk.Button(top, text="Подключить выбранное", command=self.on_connect_selected).pack(side="left", padx=6)
        self.found = ttk.Treeview(f, columns=("addr", "kind", "state"), show="headings", height=5)
        for c, t, w in (("addr", "Адрес", 220), ("kind", "Тип", 260), ("state", "Состояние", 200)):
            self.found.heading(c, text=t)
            self.found.column(c, width=w)
        self.found.pack(fill="x", pady=6)
        self.found.bind("<Double-1>", lambda e: self.on_connect_selected())

        man = ttk.LabelFrame(f, text="Вручную", padding=8)
        man.pack(fill="x", pady=4)
        ttk.Label(man, text="Адрес (IP:порт):").grid(row=0, column=0, sticky="w")
        self.addr = tk.StringVar()
        ttk.Entry(man, textvariable=self.addr, width=26).grid(row=0, column=1, padx=6)
        ttk.Button(man, text="Подключить", command=lambda: self.task(self._connect, "Подключаюсь…",
                                                                     self.addr.get().strip())).grid(row=0, column=2)
        ttk.Label(man, text="Порт 5555 — «Отладка по сети». Для «Отладки по Wi-Fi» порт с её главного экрана "
                            "(он меняется после каждой перезагрузки).", foreground="#666"
                  ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        pair = ttk.LabelFrame(f, text="Сопряжение для «Отладки по Wi-Fi» (один раз на компьютер)", padding=8)
        pair.pack(fill="x", pady=4)
        ttk.Label(pair, text="Адрес из окна «Подключить с помощью кода»:").grid(row=0, column=0, sticky="w")
        self.pair_addr, self.pair_code = tk.StringVar(), tk.StringVar()
        ttk.Entry(pair, textvariable=self.pair_addr, width=26).grid(row=0, column=1, padx=6)
        ttk.Label(pair, text="Код:").grid(row=0, column=2)
        ttk.Entry(pair, textvariable=self.pair_code, width=10).grid(row=0, column=3, padx=6)
        ttk.Button(pair, text="Сопрячь", command=self.on_pair).grid(row=0, column=4)

        self.info_var = tk.StringVar(value="Устройство не подключено.")
        box = ttk.LabelFrame(f, text="Устройство", padding=8)
        box.pack(fill="both", expand=True, pady=6)
        ttk.Label(box, textvariable=self.info_var, justify="left", wraplength=900,
                  font=("Segoe UI", 10)).pack(anchor="w")
        self.next1 = ttk.Button(f, text="Далее: что отключить →", style="Big.TButton", state="disabled",
                                command=lambda: self.nb.select(1))
        self.next1.pack(anchor="e")

    def on_scan(self):
        def scan():
            items = []
            devs = self.adb.devices()
            for serial, state in devs.items():
                if ":" in serial:
                    items.append((serial, "уже подключено", state))
            for name, kind, addr in self.adb.mdns():
                if kind.startswith("_adb-tls-pairing"):
                    continue
                label = "Отладка по Wi-Fi" if "tls-connect" in kind else "Отладка по сети"
                items.append((addr, f"{label} ({name})", devs.get(addr, "")))
            self.ui(self._fill_found, items)
            self.log(f"Найдено: {len(items)}" + ("" if items else
                     ". Если ТВ не находится, введите адрес вручную (IP есть в настройках сети ТВ)."))
        self.task(scan, "Ищу устройства…")

    def _fill_found(self, items):
        self.found.delete(*self.found.get_children())
        seen = set()
        for it in items:
            if it[0] not in seen:
                seen.add(it[0])
                self.found.insert("", "end", values=it)

    def on_connect_selected(self):
        sel = self.found.selection()
        if not sel:
            messagebox.showinfo("Выберите", "Выберите устройство в списке.")
            return
        self.task(self._connect, "Подключаюсь…", self.found.item(sel[0])["values"][0])

    def on_pair(self):
        addr, code = self.pair_addr.get().strip(), self.pair_code.get().strip()
        if not addr or not code:
            messagebox.showinfo("Сопряжение", "Введите адрес и код из окна «Подключить с помощью кода».")
            return

        def go():
            ok, out = self.adb.pair(addr, code)
            self.log(out.strip())
            if ok:
                self.log("Сопряжено. Теперь подключитесь к адресу с главного экрана «Отладки по Wi-Fi».")
                time.sleep(2)
                self.on_scan_quiet()
        self.task(go, "Сопряжение…")

    def on_scan_quiet(self):
        items = [(a, f"Отладка по Wi-Fi ({n})", "") for n, k, a in self.adb.mdns() if "tls-connect" in k]
        self.ui(self._fill_found, items)

    def _connect(self, addr):
        if not addr:
            return
        if ":" not in addr:
            addr += ":5555"
        if self.adb.devices().get(addr) != "device":
            self.log(f"Подключаюсь к {addr}. Если на ТВ появится «Разрешить отладку?» — отметьте "
                     "«Всегда разрешать» и нажмите «Разрешить».")
            ok, out = self.adb.connect(addr)
            self.log(out.strip())
            if not ok:
                self.ui(messagebox.showerror, "Не подключилось",
                        "Не удалось подключиться к " + addr + ".\n\nПроверьте, что отладка включена и адрес верный. "
                        "Для «Отладки по Wi-Fi» сначала выполните сопряжение по коду.")
                return
        self.log("Читаю сведения об устройстве…")
        info = core.probe(self.adb, addr)
        if not info["model"]:
            raise RuntimeError("Устройство не ответило на запросы. Переподключите его.")
        self.info = info
        self.log(f"{info['manufacturer']} {info['model']} ({info['device']}), Android {info['android']}, "
                 f"главный экран: {info['home']}")
        self.ui(self._after_probe)

    def _after_probe(self):
        i = self.info
        exact, similar = self.lib.match(i) if self.lib else ([], [])
        self.exact, self.similar = exact, similar
        key = core.profile_key(i)
        prof = next((p for p in core.load_profiles() if p["_key"] == key), None)
        lines = [
            f"Модель: {i['manufacturer']} {i['model']} (кодовое имя {i['device']})",
            f"Android {i['android']} (SDK {i['sdk']}), {'Google TV' if i['google_tv'] else 'Android TV'}",
            f"Главный экран сейчас: {i['home'] or '—'}",
            f"Установлено пакетов: {len(i['packages'])}, ваших приложений: {len(i['third_party'])}",
        ]
        if exact:
            lines.append("✔ Есть проверенный список для этой модели: " + ", ".join(l["folder"] for l in exact))
        elif similar:
            lines.append("Проверенного списка нет; есть списки для той же платформы: "
                         + ", ".join(l["folder"] for l in similar[:5]))
        else:
            lines.append("Проверенного списка нет — программа предложит безопасный набор, его стоит просмотреть.")
        if i["setupwraith_claims_home"]:
            lines.append("Google TV: мастер настройки перехватывает Home — он будет отключён первым.")
            if i["setupwraith_owns_wifi"]:
                lines.append("⚠ Сохранённый Wi-Fi принадлежит мастеру настройки: перед очисткой забудьте сеть "
                             "и подключитесь к ней заново в настройках ТВ, иначе ТВ потеряет Wi-Fi.")
        if i["sdk"] and i["sdk"] < 26:
            lines.append("⚠ Android ниже 8.0: лаунчер не установится, доступна только очистка.")
        if prof:
            lines.append(f"Это устройство уже обрабатывалось этой программой ({prof.get('updated', '')}). "
                         "Повторный запуск безопасен; откат — на вкладке «Откат».")
        self.info_var.set("\n".join(lines))
        for t in (1, 2):
            self.nb.tab(t, state="normal")
        self.next1.configure(state="normal")
        self._fill_plan_sources()
        self._fill_dock()
        self.restore_addr.set(i["host"])

    # ------------------------------------------------------------ tab 2: plan
    def _tab_plan(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text=" 2. Что отключить ")
        ttk.Label(f, text="Отметьте, что отключить. Ничего не удаляется, всё возвращается откатом.",
                  style="H.TLabel").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="Основа:").pack(side="left")
        self.src = tk.StringVar()
        self.src_box = ttk.Combobox(row, textvariable=self.src, state="readonly", width=70)
        self.src_box.pack(side="left", padx=6)
        self.src_box.bind("<<ComboboxSelected>>", lambda e: self._build_plan())
        ttk.Label(f, foreground="#666", wraplength=900, justify="left", text=(
            "Группы с пометкой «ваш выбор» — стриминги, ассистент, Chromecast, обновления: решайте сами. "
            "Щелчок по строке переключает галочку. Ваши собственные приложения и системное ядро в список не попадают."
        )).pack(anchor="w")
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(tf, columns=("on", "pkg", "note"), show="tree headings")
        self.tree.heading("#0", text="Группа")
        self.tree.heading("on", text="Откл.")
        self.tree.heading("pkg", text="Пакет")
        self.tree.heading("note", text="Примечание")
        self.tree.column("#0", width=260)
        self.tree.column("on", width=50, anchor="center")
        self.tree.column("pkg", width=330)
        self.tree.column("note", width=300)
        ysb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click)
        bot = ttk.Frame(f)
        bot.pack(fill="x")
        self.plan_count = tk.StringVar()
        ttk.Label(bot, textvariable=self.plan_count).pack(side="left")
        ttk.Button(bot, text="Далее: лаунчер →", style="Big.TButton",
                   command=lambda: self.nb.select(2)).pack(side="right")

    def _fill_plan_sources(self):
        opts, self.src_map = [], {}
        prof = next((p for p in core.load_profiles() if p["_key"] == core.profile_key(self.info)), None)
        if prof and prof.get("list") and Path(prof["list"]).exists():
            k = f"Мой прошлый список: {prof['list']}"
            opts.append(k)
            self.src_map[k] = ("list", {"path": Path(prof["list"])})
        for l in self.exact:
            k = f"Проверенный список: {l['folder']}/{l['path'].name} — {l['title']}"
            opts.append(k)
            self.src_map[k] = ("list", l)
        auto = "Автоподбор" + (" по похожим устройствам и статистике" if self.similar else " по статистике")
        opts.append(auto)
        self.src_map[auto] = ("auto", None)
        self.src_box["values"] = opts
        self.src.set(opts[0])
        self._build_plan()

    def _build_plan(self):
        kind, l = self.src_map[self.src.get()]
        self.rows = core.build_plan(self.info, self.lib, l["path"] if kind == "list" else None,
                                    self.similar if kind == "auto" else ())
        self.tree.delete(*self.tree.get_children())
        groups = {}
        order = sorted(self.rows, key=lambda r: (not r["choice"], r["group"]))
        for r in order:
            g = r["group"] + ("  (ваш выбор)" if r["choice"] else "")
            if g not in groups:
                groups[g] = self.tree.insert("", "end", text=g, open=True)
            r["iid"] = self.tree.insert(groups[g], "end", text="",
                                        values=(CHECK if r["checked"] else UNCHECK, r["pkg"], r["note"]))
        self._update_count()

    def on_tree_click(self, e):
        iid = self.tree.identify_row(e.y)
        r = next((r for r in self.rows if r.get("iid") == iid), None)
        if r:
            r["checked"] = not r["checked"]
            self.tree.set(iid, "on", CHECK if r["checked"] else UNCHECK)
            self._update_count()
        elif iid:  # a group row: toggle all of it
            kids = [r for r in self.rows if self.tree.parent(r["iid"]) == iid]
            val = not all(r["checked"] for r in kids)
            for r in kids:
                r["checked"] = val
                self.tree.set(r["iid"], "on", CHECK if val else UNCHECK)
            self._update_count()

    def _update_count(self):
        n = sum(r["checked"] for r in self.rows)
        self.plan_count.set(f"Будет отключено: {n} из {len(self.rows)}")

    # ------------------------------------------------------------ tab 3: launcher
    def _tab_launcher(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text=" 3. Лаунчер ")
        self.opt_launcher = tk.BooleanVar(value=True)
        self.opt_clock = tk.BooleanVar(value=True)
        self.opt_ota = tk.BooleanVar(value=True)
        self.opt_gear = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, variable=self.opt_launcher, text="Установить чистый лаунчер и сделать его главным экраном "
                        "(стоковый отключается только после этого)").pack(anchor="w")
        ttk.Checkbutton(f, variable=self.opt_clock, text="Заставка — часы лаунчера").pack(anchor="w")
        ttk.Checkbutton(f, variable=self.opt_ota, text="Запретить автоматическую установку обновлений системы"
                        ).pack(anchor="w")
        ttk.Checkbutton(f, variable=self.opt_gear, text="Кнопка-шестерёнка пульта открывает панель быстрых настроек "
                        "(пульты Google TV, где она перестаёт работать без стокового лаунчера)").pack(anchor="w")

        ttk.Label(f, text="Нижняя панель (док): выберите приложения по порядку, 4–8 штук",
                  style="H.TLabel").pack(anchor="w", pady=(14, 4))
        row = ttk.Frame(f)
        row.pack(fill="both", expand=True)
        self.avail = tk.Listbox(row, height=14, exportselection=False)
        self.avail.pack(side="left", fill="both", expand=True)
        mid = ttk.Frame(row)
        mid.pack(side="left", padx=8)
        for txt, cmd in (("Добавить →", self.dock_add), ("← Убрать", self.dock_remove),
                         ("↑ Выше", lambda: self.dock_move(-1)), ("↓ Ниже", lambda: self.dock_move(1))):
            ttk.Button(mid, text=txt, command=cmd, width=12).pack(pady=3)
        self.dock = tk.Listbox(row, height=14, exportselection=False)
        self.dock.pack(side="left", fill="both", expand=True)
        self.avail.bind("<Double-1>", lambda e: self.dock_add())
        self.dock.bind("<Double-1>", lambda e: self.dock_remove())
        ttk.Button(f, text="Далее: запуск →", style="Big.TButton",
                   command=self.go_run_tab).pack(anchor="e", pady=8)

    def _fill_dock(self):
        i = self.info
        apps = [p for p in i["tv_apps"] if p != core.LAUNCHER_PKG]
        apps.sort(key=lambda p: (p not in i["third_party"], p))
        self.avail.delete(0, "end")
        self.dock.delete(0, "end")
        for p in apps:
            self.avail.insert("end", p)
        for p in [p for p in apps if p in i["third_party"]][:4]:
            self.dock.insert("end", p)

    def dock_add(self):
        for idx in self.avail.curselection():
            p = self.avail.get(idx)
            if p not in self.dock.get(0, "end") and self.dock.size() < 8:
                self.dock.insert("end", p)

    def dock_remove(self):
        for idx in reversed(self.dock.curselection()):
            self.dock.delete(idx)

    def dock_move(self, d):
        sel = self.dock.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + d
        if 0 <= j < self.dock.size():
            p = self.dock.get(i)
            self.dock.delete(i)
            self.dock.insert(j, p)
            self.dock.selection_set(j)

    def go_run_tab(self):
        self.nb.tab(3, state="normal")
        n = sum(r["checked"] for r in self.rows)
        dock = list(self.dock.get(0, "end"))
        parts = [f"Устройство: {self.info['model']} ({self.info['host']})",
                 f"Отключить пакетов: {n}"]
        if self.opt_launcher.get():
            parts.append("Лаунчер: установить, док — " + (", ".join(dock) or "по умолчанию"))
        if self.opt_clock.get():
            parts.append("Заставка: часы")
        if self.opt_ota.get():
            parts.append("Автообновления системы: выключить")
        if self.opt_gear.get():
            parts.append("Шестерёнка: панель быстрых настроек")
        self.summary.set("\n".join(parts))
        self.nb.select(3)

    # ------------------------------------------------------------ tab 4: run
    def _tab_run(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text=" 4. Запуск ")
        ttk.Label(f, text="Проверьте и запускайте", style="H.TLabel").pack(anchor="w")
        self.summary = tk.StringVar()
        ttk.Label(f, textvariable=self.summary, justify="left", font=("Segoe UI", 10)).pack(anchor="w", pady=8)
        ttk.Label(f, foreground="#666", wraplength=900, justify="left", text=(
            "Перед изменениями сохраняется бэкап и запись для отката. Не выключайте ТВ, пока идёт работа. "
            "Ход работы — в журнале внизу.")).pack(anchor="w")
        btns = ttk.Frame(f)
        btns.pack(anchor="w", pady=12)
        ttk.Button(btns, text="Начать очистку", style="Big.TButton", command=self.on_run).pack(side="left")
        self.reboot_btn = ttk.Button(btns, text="Перезагрузить и проверить", style="Big.TButton",
                                     state="disabled", command=self.on_reboot)
        self.reboot_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Проверить сейчас", command=lambda: self.task(self._verify, "Проверяю…")
                   ).pack(side="left")
        self.result = tk.StringVar()
        ttk.Label(f, textvariable=self.result, justify="left", font=("Segoe UI", 11, "bold"),
                  wraplength=900).pack(anchor="w", pady=8)

    def on_run(self):
        if not messagebox.askyesno("Начать?", self.summary.get() + "\n\nНачать?"):
            return
        self.task(self._run, "Очистка…", [r for r in self.rows if r["checked"]], list(self.dock.get(0, "end")),
                  self.opt_launcher.get(), self.opt_clock.get(), self.opt_ota.get(), self.opt_gear.get())

    def _run(self, chosen, dock, do_launcher, do_clock, do_ota, do_gear):
        i, cfg, host = self.info, self.cfg, self.info["host"]
        key = core.profile_key(i)
        pdir = core.profile_dir(key)
        prev = next((p for p in core.load_profiles() if p["_key"] == key), None)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        self.log("— Бэкап")
        b = core.backup(self.adb, host, pdir / f"backup-{stamp}")
        self.log(f"Сохранено: {b}")
        list_path = Path(prev["list"]) if prev and prev.get("list") else pdir / "debloat.txt"
        extra_lists = list(prev.get("extra_lists", [])) if prev else []
        run_lists = []
        if chosen:
            if pdir in list_path.parents or not list_path.exists():
                core.write_list(list_path, i, chosen)  # our own file: rewrite it
                run_lists.append(list_path)
            else:
                # A list we did not write (the repo's or a hand-made one): never edit it. Its record for
                # the undo is keyed to that file, so new packages go into a separate list of their own.
                old = {p for p, _, _, a in core.parse_list(list_path) if a}
                run_lists.append(list_path)
                added = [r for r in chosen if r["pkg"] not in old]
                if added:
                    extra = pdir / "debloat-extra.txt"
                    core.write_list(extra, i, added)
                    run_lists.append(extra)
                    if str(extra) not in extra_lists:
                        extra_lists.append(str(extra))
                dropped = old - {r["pkg"] for r in chosen}
                if dropped:
                    self.log("Снятые галочки не включают пакеты обратно (это делает «Откат»): "
                             + ", ".join(sorted(dropped)))
            self.log("Списки: " + ", ".join(map(str, run_lists)))
        profile = prev or {
            "model": i["model"], "device": i["device"], "manufacturer": i["manufacturer"],
            "android": i["android"], "fingerprint": i["fingerprint"],
            "before": {"home": i["home"], "stock_launcher": i["stock_launcher"],
                       "screensaver": i["screensaver"], "accessibility": i["accessibility"],
                       "accessibility_enabled": i["accessibility_enabled"], "ota_flag": i["ota_flag"]},
            "extra_disabled": [],
        }
        profile.pop("_key", None)
        profile.update(host=host, list=str(list_path), extra_lists=extra_lists, updated=stamp, launcher=do_launcher or
                       profile.get("launcher", False), google_tv=i["google_tv"])
        core.save_profile(key, profile)

        for lp in run_lists:
            self.log(f"— Отключение пакетов ({Path(lp).name})")
            rc, out = self.stream(core.ps_script(cfg, "tools/disable.ps1", "-TvHost", host, lp),
                                  env=core.script_env(cfg))
            if rc != 0:
                raise RuntimeError("Скрипт отключения остановился (подробности в журнале). "
                                   "Устройство в рабочем состоянии, откат доступен.")
        if chosen:
            if any(r["pkg"] == core.SETUPWRAITH for r in chosen):
                dis = self.adb.shell(host, "pm list packages -d")
                if core.SETUPWRAITH not in dis:
                    raise RuntimeError("Мастер настройки не отключён (ему принадлежит сохранённый Wi-Fi). "
                                       "Забудьте сеть Wi-Fi и подключитесь заново в настройках ТВ, затем "
                                       "запустите очистку ещё раз. Лаунчер пока не ставился.")

        if do_launcher:
            if i["sdk"] and i["sdk"] < 26:
                self.log("Android ниже 8.0 — лаунчер пропущен.")
            else:
                self.log("— Установка лаунчера")
                skip = core.apk_path(cfg).exists()
                # -RestoreKeyServices: other apps that switch accessibility services on or off (remote
                # control apps, button mappers) or a force stop can drop the key service; with this the
                # launcher puts it back the next time Home opens.
                args = ["-TvHost", host] + (["-HomeKeyService", "-RestoreKeyServices"] if do_gear else [])
                rc, out = self.stream(core.ps_script(cfg, "launcher/build-and-install.ps1", *args),
                                      env=core.script_env(cfg, skip_build=skip),
                                      cwd=str(Path(cfg["repo"]) / "launcher"))
                home = self.adb.shell(host, "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                                            "-c android.intent.category.HOME").splitlines()
                if rc != 0 or not home or not home[-1].startswith(core.LAUNCHER_PKG):
                    raise RuntimeError("Лаунчер не стал главным экраном (подробности в журнале). "
                                       "Стоковый лаунчер при этом не отключался.")
                theme = {"app_keys": []}
                if dock:
                    theme["dock_apps"] = dock
                    if len(dock) > 4:
                        theme["columns"] = len(dock)
                if do_gear:
                    theme.update(settings_key_keycodes=[83], settings_key_panel=True, restore_key_services=True)
                tdir = pdir / "theme"
                tdir.mkdir(parents=True, exist_ok=True)
                (tdir / "config.json").write_text(json.dumps(theme, indent=2), encoding="utf-8")
                self.log("— Тема (док)")
                rc, _ = self.stream(core.ps_script(cfg, "tools/theme.ps1", "-TvHost", host, tdir),
                                    env=core.script_env(cfg))
                if rc != 0:
                    self.log("Тема не применилась; лаунчер работает с настройками по умолчанию.")
                if do_clock:
                    self.adb.shell(host, f"settings put secure screensaver_components {core.CLOCK_DREAM}")
                    self.adb.shell(host, f"settings put secure screensaver_default_component {core.CLOCK_DREAM}")
                    self.log("Заставка: часы лаунчера")
        if do_ota:
            self.adb.shell(host, "settings put global ota_disable_automatic_update 1")
            self.log("Автоустановка обновлений системы выключена")
        self.adb.shell(host, "sync")
        self.log("Готово. Перезагрузите устройство кнопкой «Перезагрузить и проверить».")
        self.ui(self.reboot_btn.configure, state="normal")
        self.ui(self.result.set, "Очистка выполнена. Осталось перезагрузить и проверить.")
        self.ui(self._refresh_profiles)

    def on_reboot(self):
        self.task(self._reboot, "Перезагрузка…")

    def _reboot(self):
        host = self.info["host"]
        self.log("Жду 15 секунд, чтобы Android записал изменения на диск…")
        time.sleep(15)
        self.adb.shell(host, "sync")
        self.adb.run("-s", host, "shell", "svc power reboot", timeout=15)
        self.log("Перезагрузка отправлена.")
        if not host.endswith(":5555"):
            self.ui(self.result.set, "Устройство перезагружается. «Отладка по Wi-Fi» после перезагрузки выключается "
                                     "и получает новый порт: включите её, подключитесь на вкладке 1 "
                                     "и нажмите «Проверить сейчас».")
            return
        time.sleep(30)
        for _ in range(40):
            self.adb.run("connect", host, timeout=10)
            if self.adb.devices().get(host) == "device" and \
                    self.adb.shell(host, "getprop sys.boot_completed") == "1":
                time.sleep(8)
                self._verify()
                return
            time.sleep(5)
        self.ui(self.result.set, "Устройство не вернулось в сеть за 4 минуты. Если ТВ загрузился, включите отладку "
                                 "и нажмите «Проверить сейчас».")

    def _verify(self):
        i = self.info
        host = i["host"]
        if self.adb.devices().get(host) != "device":
            self.ui(self.result.set, "Нет связи с устройством: подключитесь на вкладке 1.")
            return
        prof = next((p for p in core.load_profiles() if p["_key"] == core.profile_key(i)), {})
        want = set()
        for lp in [prof.get("list")] + prof.get("extra_lists", []):
            if lp and Path(lp).exists():
                want |= {p for p, _, _, a in core.parse_list(lp) if a} & i["packages"]
        dis = {l.split(":", 1)[1] for l in self.adb.shell(host, "pm list packages -d").splitlines() if ":" in l}
        home = self.adb.shell(host, "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                                    "-c android.intent.category.HOME").splitlines()
        home = home[-1] if home else "?"
        back = sorted(want - dis)
        msg = [f"Главный экран: {home}",
               f"Отключено из списка: {len(want) - len(back)} из {len(want)}"]
        if back:
            msg.append("Включились обратно (прошивка их возвращает): " + ", ".join(back))
        ok = (not prof.get("launcher") or home.startswith(core.LAUNCHER_PKG)) and not back
        msg.insert(0, "✔ Всё на месте." if ok else "Есть расхождения:")
        self.log("\n".join(msg))
        self.ui(self.result.set, "\n".join(msg))

    # ------------------------------------------------------------ tab 5: restore
    def _tab_restore(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text=" Откат ")
        ttk.Label(f, text="Вернуть устройство как было", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, foreground="#666", wraplength=900, justify="left", text=(
            "Возвращает отключённые пакеты, стоковый лаунчер, заставку, службы спецвозможностей и настройку "
            "обновлений — по записи, сделанной до очистки. Устройство должно быть подключено (вкладка 1)."
        )).pack(anchor="w", pady=(2, 8))
        self.prof_tree = ttk.Treeview(f, columns=("model", "host", "list", "date"), show="headings", height=6)
        for c, t, w in (("model", "Устройство", 220), ("host", "Последний адрес", 160),
                        ("list", "Список", 380), ("date", "Изменено", 130)):
            self.prof_tree.heading(c, text=t)
            self.prof_tree.column(c, width=w)
        self.prof_tree.pack(fill="x")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        ttk.Label(row, text="Текущий адрес устройства:").pack(side="left")
        self.restore_addr = tk.StringVar()
        ttk.Entry(row, textvariable=self.restore_addr, width=26).pack(side="left", padx=6)
        self.opt_uninstall = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, variable=self.opt_uninstall, text="Также удалить лаунчер CleanYourTV"
                        ).pack(anchor="w")
        ttk.Button(f, text="Откатить", style="Big.TButton", command=self.on_restore).pack(anchor="w", pady=8)
        self.prof_tree.bind("<<TreeviewSelect>>", self._on_prof_select)

    def _refresh_profiles(self):
        self.profiles = core.load_profiles()
        self.prof_tree.delete(*self.prof_tree.get_children())
        for p in self.profiles:
            self.prof_tree.insert("", "end", iid=p["_key"], values=(
                f"{p.get('manufacturer', '')} {p.get('model', '')}", p.get("host", ""),
                p.get("list", ""), p.get("updated", "")))

    def _on_prof_select(self, e):
        sel = self.prof_tree.selection()
        if sel:
            p = next(p for p in self.profiles if p["_key"] == sel[0])
            if not (self.info and core.profile_key(self.info) == sel[0]):
                self.restore_addr.set(p.get("host", ""))

    def on_restore(self):
        sel = self.prof_tree.selection()
        if not sel:
            messagebox.showinfo("Откат", "Выберите устройство в списке.")
            return
        p = next(p for p in self.profiles if p["_key"] == sel[0])
        host = self.restore_addr.get().strip()
        if not messagebox.askyesno("Откат", f"Вернуть {p.get('model')} ({host}) в исходное состояние?"):
            return
        self.task(self._restore, "Откат…", p, host, self.opt_uninstall.get())

    def _restore(self, p, host, uninstall):
        cfg = self.cfg
        if ":" not in host:
            host += ":5555"
        if self.adb.devices().get(host) != "device":
            ok, out = self.adb.connect(host)
            self.log(out.strip())
            if not ok:
                raise RuntimeError(f"Нет связи с {host}. Включите отладку и укажите текущий адрес.")
        model = self.adb.shell(host, "getprop ro.product.model")
        if model != p.get("model"):
            raise RuntimeError(f"По адресу {host} другое устройство ({model}), а не {p.get('model')}.")
        before = p.get("before", {})
        stock = before.get("stock_launcher") or "com.google.android.tvlauncher"
        for lp in list(reversed(p.get("extra_lists", []))) + ([p["list"]] if p.get("list") else []):
            if not Path(lp).exists():
                self.log(f"Нет файла списка {lp}, пропускаю")
                continue
            self.log(f"— Возврат пакетов ({Path(lp).name})")
            rc, _ = self.stream(core.ps_script(cfg, "tools/restore.ps1", lp, "-Stock", stock,
                                               "-TvHost", host), env=core.script_env(cfg))
            if rc != 0:
                raise RuntimeError("Скрипт отката остановился (подробности в журнале).")
        for pkg in p.get("extra_disabled", []):
            self.log(self.adb.shell(host, f"pm enable {pkg}"))
        if self.adb.shell(host, f"pm path {core.LAUNCHER_PKG}"):
            self.stream(core.ps_script(cfg, "tools/theme.ps1", "-TvHost", host, "-Reset"), env=core.script_env(cfg))

        def put(ns, name, val):
            if val in (None, "", "null"):
                self.adb.shell(host, f"settings delete {ns} {name}")
            else:
                self.adb.shell(host, f"settings put {ns} {name} '{val}'")
        put("secure", "screensaver_components", before.get("screensaver"))
        put("secure", "enabled_accessibility_services", before.get("accessibility"))
        put("secure", "accessibility_enabled", before.get("accessibility_enabled"))
        put("global", "ota_disable_automatic_update", before.get("ota_flag"))
        self.log("Настройки заставки, спецвозможностей и обновлений возвращены.")
        home = self.adb.shell(host, "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                                    "-c android.intent.category.HOME").splitlines()
        home = home[-1] if home else ""
        self.log(f"Главный экран: {home}")
        if uninstall:
            if home.startswith(core.LAUNCHER_PKG):
                self.log("Лаунчер не удалён: он всё ещё главный экран.")
            else:
                self.log(self.adb.shell(host, f"pm uninstall {core.LAUNCHER_PKG}"))
        self.adb.shell(host, "sync")
        self.ui(messagebox.showinfo, "Откат", "Готово. Подождите 15 секунд и перезагрузите устройство.")


if __name__ == "__main__":
    App().mainloop()
