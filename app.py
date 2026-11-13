"""Avero — управление приложениями iPhone с Windows (аналог iMazing): установка, библиотека IPA,
документы приложений, бэкап данных, автообновление с GitHub."""
import asyncio, datetime, io, json, os, plistlib, re, shutil, socket, subprocess, sys, threading, \
    urllib.request, uuid, webbrowser, zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

if sys.stdout is None or sys.stderr is None:  # exe без консоли: прогресс-бары и логи пишем в никуда
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3 import exceptions as E

__version__ = "dev"
REPO = "mrbean13140-tech/Avero"
PORT = int(os.environ.get("PORT", 8765))
HOST = os.environ.get("HOST", "0.0.0.0")
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Avero")
LIB = os.path.join(DATA, "library")
BAK = os.path.join(DATA, "backups")
for _d in (LIB, BAK):
    os.makedirs(_d, exist_ok=True)
APPLE_DEVICES = "https://apps.microsoft.com/detail/9NP83LWLPZ9K"
SKIP = {".com.apple.mobile_container_manager.metadata.plist"}
JOBS = {}

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def call(coro, timeout=600):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


def job_start(title, coro_fn):
    """Долгая операция в фоне: прогресс пишется в JOBS[job]['log']."""
    job = uuid.uuid4().hex[:8]
    JOBS[job] = {"state": "running", "log": title}

    async def runner():
        try:
            JOBS[job].update(state="ok", log=await coro_fn(job) or "Готово")
        except Exception as e:
            JOBS[job].update(state="error", log=human(e))
    asyncio.run_coroutine_threadsafe(runner(), LOOP)
    return job


def human(e: Exception) -> str:
    if isinstance(e, ConnectionRefusedError) or "27015" in str(e) or "usbmuxd" in str(e).lower():
        return ("Не найдена служба Apple Mobile Device. Установите «Apple Devices» из Microsoft Store "
                f"({APPLE_DEVICES}) или iTunes, затем переподключите iPhone.")
    if isinstance(e, E.PasswordRequiredError):
        return "Разблокируйте iPhone и нажмите «Доверять этому компьютеру»."
    if isinstance(e, (E.NotTrustedError, E.PairingError, E.NotPairedError, E.FatalPairingError)):
        return "iPhone не доверяет этому ПК: разблокируйте его, нажмите «Доверять» и повторите."
    if isinstance(e, (E.NoDeviceConnectedError, E.ConnectionFailedError)):
        return "Устройство не найдено — переподключите кабель."
    if isinstance(e, E.AppInstallError):
        m = str(e)
        if "ApplicationVerificationFailed" in m or "signature" in m.lower():
            return "iOS отклонил подпись IPA: файл не подписан для этого устройства. " + m[:300]
        return "Ошибка установки: " + m[:500]
    if "refused to open the container" in str(e) or "InstallationLookupFailed" in str(e):
        return ("iOS не даёт доступ к данным этого приложения. Полный доступ есть только у приложений, "
                "подписанных dev-профилем; для остальных доступны лишь «Файлы» (если приложение их открывает).")
    return f"{type(e).__name__}: {e}"[:600]


async def lockdown(udid):
    return await create_using_usbmux(serial=udid, pair_timeout=60)


# ---------- устройства и приложения ----------

async def _devices():
    res = {}
    for d in await list_devices():
        if d.serial in res and res[d.serial]["conn"] == "USB":
            continue
        info = {"udid": d.serial, "conn": d.connection_type, "name": "iPhone", "model": "", "ios": ""}
        try:
            async with await create_using_usbmux(serial=d.serial, autopair=False) as ld:
                v = ld.short_info
                info.update(name=v.get("DeviceName", "iPhone"), model=v.get("ProductType", ""),
                            ios=v.get("ProductVersion", ""))
        except Exception:
            info["name"] = "iPhone (нажмите «Доверять»)"
        res[d.serial] = info
    return list(res.values())


async def _apps(udid):
    async with await lockdown(udid) as ld:
        async with InstallationProxyService(lockdown=ld) as ip:
            data = await ip.get_apps(application_type="User", calculate_sizes=True)
    out = []
    for b, a in data.items():
        ent = a.get("Entitlements") or {}
        out.append({"id": b, "name": a.get("CFBundleDisplayName") or a.get("CFBundleName") or b,
                    "ver": a.get("CFBundleShortVersionString", ""),
                    "size": int(a.get("StaticDiskUsage") or 0) + int(a.get("DynamicDiskUsage") or 0),
                    "files": bool(a.get("UIFileSharingEnabled")),
                    "dev": bool(ent.get("get-task-allow")),
                    # App Store: есть метаданные покупки/подпись Apple, либо нет ни dev-подписи, ни профиля (sideload)
                    "store": bool(a.get("iTunesMetadata") or a.get("ApplicationDSID")
                                  or "Apple iPhone OS Application Signing" in str(a.get("SignerIdentity", ""))
                                  or (not ent.get("get-task-allow") and not a.get("ProfileValidated")))})
    return sorted(out, key=lambda x: x["name"].lower())


async def _uninstall(udid, bid):
    async with await lockdown(udid) as ld:
        async with InstallationProxyService(lockdown=ld) as ip:
            await ip.uninstall(bid)
    return {"log": "Удалено"}


# ---------- библиотека IPA ----------

def ipa_meta(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n))
        p = plistlib.loads(z.read(name))
    return {"id": p.get("CFBundleIdentifier", "unknown"), "ver": p.get("CFBundleShortVersionString", ""),
            "name": p.get("CFBundleDisplayName") or p.get("CFBundleName") or p.get("CFBundleIdentifier", "")}


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "file"


def lib_save(data: bytes):
    m = ipa_meta(data)
    fn = safe_name(f"{m['name']} {m['ver']} ({m['id']}).ipa")
    with open(os.path.join(LIB, fn), "wb") as f:
        f.write(data)
    with open(os.path.join(LIB, fn + ".json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return fn


def lib_list():
    out = []
    for fn in sorted(os.listdir(LIB)):
        if not fn.lower().endswith(".ipa"):
            continue
        p = os.path.join(LIB, fn)
        try:
            with open(p + ".json", encoding="utf-8") as f:
                m = json.load(f)
        except OSError:
            m = {"name": fn, "id": "", "ver": ""}
        out.append({**m, "file": fn, "size": os.path.getsize(p),
                    "date": datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d.%m.%Y")})
    return out


def lib_path(fn, root=LIB):
    p = os.path.join(root, os.path.basename(fn))
    if not os.path.isfile(p):
        raise FileNotFoundError("Файл не найден в библиотеке")
    return p


async def install_bytes(job, udid, data):
    def progress(p, *_):
        JOBS[job]["log"] = f"Установка… {p}%"
    async with await lockdown(udid) as ld:
        async with InstallationProxyService(lockdown=ld) as ip:
            JOBS[job]["log"] = "Загрузка на устройство…"
            await ip.install_from_bytes(data, handler=progress)
    return "Установлено"


# ---------- документы приложения (File Sharing) ----------

def dpath(p):
    p = "/" + "/".join(x for x in (p or "").split("/") if x not in ("", ".", ".."))
    return "/Documents" + (p if p != "/" else "")


async def _docs_list(udid, bid, path):
    async with await lockdown(udid) as ld:
        ha = await HouseArrestService.create(ld, bid, documents_only=True)
        async with ha:
            root = dpath(path)
            items = []
            for n in await ha.listdir(root):
                if n in (".", ".."):
                    continue
                st = await ha.stat(f"{root}/{n}")
                items.append({"name": n, "dir": st.get("st_ifmt") == "S_IFDIR", "size": int(st.get("st_size", 0))})
    return sorted(items, key=lambda x: (not x["dir"], x["name"].lower()))


async def _docs_get(udid, bid, path):
    async with await lockdown(udid) as ld:
        ha = await HouseArrestService.create(ld, bid, documents_only=True)
        async with ha:
            return await ha.get_file_contents(dpath(path))


async def _docs_put(udid, bid, path, data):
    async with await lockdown(udid) as ld:
        ha = await HouseArrestService.create(ld, bid, documents_only=True)
        async with ha:
            await ha.set_file_contents(dpath(path), data)
    return {"log": "Загружено"}


async def _docs_rm(udid, bid, path):
    async with await lockdown(udid) as ld:
        ha = await HouseArrestService.create(ld, bid, documents_only=True)
        async with ha:
            await ha.rm(dpath(path), force=True)
    return {"log": "Удалено"}


# ---------- бэкап данных приложения ----------

async def backup_app(job, udid, bid, name):
    fn = safe_name(f"{name} ({bid}) {datetime.datetime.now():%Y-%m-%d %H-%M}.averoapp")
    tmp = os.path.join(BAK, fn + ".part")
    n = 0
    async with await lockdown(udid) as ld:
        ha = await HouseArrestService.create(ld, bid, documents_only=False)
        async with ha:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("avero.json", json.dumps({"id": bid, "name": name}, ensure_ascii=False))
                async for root, _dirs, files in ha.walk("/"):
                    for f in files:
                        rp = f"{root.rstrip('/')}/{f}"
                        if f in SKIP:
                            continue
                        try:
                            z.writestr("container" + rp, await ha.get_file_contents(rp))
                            n += 1
                            JOBS[job]["log"] = f"Бэкап… файлов: {n}"
                        except Exception:
                            pass  # симлинки и системные файлы пропускаем
    os.replace(tmp, os.path.join(BAK, fn))
    return f"Бэкап сохранён: {fn} ({n} файлов)"


def bak_list():
    out = []
    for fn in sorted(os.listdir(BAK), reverse=True):
        if not fn.endswith(".averoapp"):
            continue
        p = os.path.join(BAK, fn)
        try:
            with zipfile.ZipFile(p) as z:
                m = json.loads(z.read("avero.json"))
        except Exception:
            m = {"id": "", "name": fn}
        out.append({**m, "file": fn, "size": os.path.getsize(p),
                    "date": datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d.%m.%Y %H:%M")})
    return out


async def restore_app(job, udid, fn):
    p = lib_path(fn, BAK)
    with zipfile.ZipFile(p) as z:
        bid = json.loads(z.read("avero.json"))["id"]
        entries = [i for i in z.infolist() if i.filename.startswith("container/") and not i.is_dir()]
        async with await lockdown(udid) as ld:
            ha = await HouseArrestService.create(ld, bid, documents_only=False)
            async with ha:
                for k, i in enumerate(entries, 1):
                    rp = i.filename[len("container"):]
                    await ha.makedirs(os.path.dirname(rp).replace("\\", "/") or "/")
                    await ha.set_file_contents(rp, z.read(i))
                    JOBS[job]["log"] = f"Восстановление… {k}/{len(entries)}"
    return f"Данные восстановлены ({len(entries)} файлов). Перезапустите приложение на iPhone."


# ---------- App Store (через встроенный ipatool) ----------

IPATOOL = os.path.join(BASE, "ipatool.exe" if os.name == "nt" else "ipatool")
SETTINGS = os.path.join(DATA, "settings.json")
NETERR = ("eof", "connection reset", "timeout", "no such host", "connectex", "tls handshake", "forcibly closed")


def settings(new=None):
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    if new is not None:
        cur.update(new)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
    return cur


def ipatool(*args, timeout=600, attempts=3):
    for i in range(attempts):  # обрывы связи с Apple бывают разовыми — повторяем
        code, rows, err = _ipatool(args, timeout)
        if not err or not any(x in err.lower() for x in NETERR):
            break
    return code, rows, err


def _ipatool(args, timeout):
    if not os.path.exists(IPATOOL):
        raise RuntimeError("ipatool не найден в сборке")
    env = dict(os.environ)
    proxy = settings().get("proxy", "").strip()
    if proxy:
        if "://" not in proxy:
            proxy = "http://" + proxy
        env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, https_proxy=proxy, http_proxy=proxy)
    p = subprocess.run([IPATOOL, *args, "--format", "json", "--non-interactive", "--keychain-passphrase", "avero"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                       creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW
    rows = []
    for line in (p.stdout + "\n" + p.stderr).splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            if line.strip():
                rows.append({"message": line.strip()})
    err = next((r.get("error") or r.get("message") for r in rows if r.get("level") == "error" or r.get("error")), None)
    return p.returncode, rows, err


def as_error(err):
    m = (err or "").lower()
    if any(x in m for x in NETERR) or "transport" in m:
        return ("Нет связи с App Store (обрыв соединения с серверами Apple). В России провайдеры часто режут App Store: "
                "укажите прокси ниже (например http://IP:порт или socks5://IP:порт вашего VPN) или пустите домены "
                "itunes.apple.com и apple.com через VPN на роутере.")
    if "auth code" in m or "2fa" in m:
        return "Нужен код двухфакторной аутентификации"
    if "not logged" in m or "keychain" in m or "sign in" in m or "account" in m and "not" in m:
        return "Войдите в Apple ID (кнопка «Apple ID» сверху)"
    if "license" in m or "purchase" in m:
        return "Приложение не куплено на этом Apple ID: " + (err or "")[:200]
    if "password" in m or "credentials" in m or "badlogin" in m:
        return "Неверный Apple ID или пароль"
    return (err or "Неизвестная ошибка ipatool")[:400]


def as_status():
    code, rows, err = ipatool("auth", "info", timeout=60)
    acc = next((r for r in rows if r.get("email")), None)
    return {"logged": bool(acc) and code == 0, "email": (acc or {}).get("email", ""), "name": (acc or {}).get("name", "")}


def as_login(email, password, code=""):
    args = ["auth", "login", "-e", email, "-p", password] + (["--auth-code", code] if code else [])
    rc, rows, err = ipatool(*args, timeout=300)
    if any("2FA code is required" in str(r.get("message", "")) for r in rows):
        return {"ok": False, "need2fa": True, "error": "Введите код, пришедший на ваши устройства Apple"}
    if rc == 0 and any(r.get("success") for r in rows):
        return {"ok": True, **as_status()}
    return {"ok": False, "error": as_error(err)}


def as_logout():
    ipatool("auth", "revoke", timeout=60)
    return {"log": "Вы вышли из Apple ID"}


async def as_download(job, bid):
    out_dir = os.path.join(DATA, "tmp")
    os.makedirs(out_dir, exist_ok=True)
    JOBS[job]["log"] = "Скачивание из App Store… (может занять несколько минут)"
    rc, rows, err = await asyncio.to_thread(ipatool, "download", "-b", bid, "--purchase", "-o", out_dir + os.sep, timeout=3600)
    path = next((r.get("output") for r in rows if r.get("output")), None)
    if rc != 0 or not path or not os.path.exists(path):
        raise RuntimeError(as_error(err))
    with open(path, "rb") as f:
        fn = lib_save(f.read())
    os.remove(path)
    return f"IPA сохранён в библиотеку: {fn}. Он зашифрован App Store и ставится на устройства с этим же Apple ID."


# ---------- автообновление ----------

def gh_latest():
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "Avero", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.load(r)
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
    return rel.get("tag_name", "").lstrip("v"), asset, rel.get("body", "")


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def update_check():
    latest, asset, notes = gh_latest()
    return {"current": __version__, "latest": latest, "notes": notes[:1500],
            "available": bool(asset) and __version__ != "dev" and vtuple(latest) > vtuple(__version__),
            "frozen": getattr(sys, "frozen", False)}


async def update_apply(job):
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Обновление работает только в собранном Avero.exe")
    latest, asset, _ = await asyncio.to_thread(gh_latest)
    if not asset:
        raise RuntimeError("В релизе нет exe")
    exe = sys.executable
    new = exe + ".new"

    def download():
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "Avero"})
        with urllib.request.urlopen(req, timeout=60) as r, open(new, "wb") as f:
            total, done = int(r.headers.get("Content-Length") or asset.get("size") or 0), 0
            while chunk := r.read(1 << 20):
                f.write(chunk); done += len(chunk)
                if total:
                    JOBS[job]["log"] = f"Скачивание {latest}… {done * 100 // total}%"
    await asyncio.to_thread(download)
    old = exe + ".old"
    if os.path.exists(old):
        os.remove(old)
    os.replace(exe, old)      # запущенный exe на Windows можно переименовать, но не перезаписать
    os.replace(new, exe)
    subprocess.Popen([exe, "--updated"], close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
    LOOP.call_later(1.5, lambda: os._exit(0))
    return f"Обновлено до {latest}, перезапуск…"


def cleanup_old():
    try:
        os.remove(sys.executable + ".old")
    except OSError:
        pass


def open_folder(p):
    if os.name == "nt":
        os.startfile(p)  # noqa
    else:
        subprocess.Popen(["xdg-open", p])


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json; charset=utf-8", fname=None):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        if fname:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(fname)}")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def safe(self, fn):
        try:
            r = fn()
            self.send(200, call(r) if asyncio.iscoroutine(r) else r)
        except Exception as e:
            self.send(500, {"error": human(e)})

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        g = u.path
        if g == "/":
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self.send(200, f.read().replace(b"{{VERSION}}", __version__.encode()), "text/html; charset=utf-8")
        if g == "/api/info": return self.send(200, {"phone": f"http://{lan_ip()}:{PORT}", "version": __version__})
        if g == "/api/devices": return self.safe(_devices)
        if g == "/api/apps": return self.safe(lambda: _apps(q["udid"]))
        if g == "/api/job": return self.send(200, JOBS.get(q.get("id"), {"state": "error", "log": "нет задачи"}))
        if g == "/api/lib": return self.safe(lib_list)
        if g == "/api/backups": return self.safe(bak_list)
        if g == "/api/docs": return self.safe(lambda: _docs_list(q["udid"], q["id"], q.get("path", "")))
        if g == "/api/update": return self.safe(update_check)
        if g == "/api/as/status": return self.safe(as_status)
        if g == "/api/settings": return self.send(200, settings())
        if g in ("/api/lib/file", "/api/backup/file", "/api/docs/file"):
            try:
                if g == "/api/docs/file":
                    data, name = call(_docs_get(q["udid"], q["id"], q["path"])), q["path"].split("/")[-1]
                else:
                    p = lib_path(q["file"], LIB if g == "/api/lib/file" else BAK)
                    data, name = open(p, "rb").read(), os.path.basename(p)
                return self.send(200, data, "application/octet-stream", fname=name)
            except Exception as e:
                return self.send(500, {"error": human(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        g = u.path
        if g in ("/api/install", "/api/lib/add"):
            name = os.path.basename(unquote(self.headers.get("X-Filename", "app.ipa")))
            if not name.lower().endswith(".ipa"):
                return self.send(400, {"error": "Нужен файл .ipa"})
            data = self.body()
            try:
                fn = lib_save(data)
            except Exception:
                return self.send(400, {"error": "Это не похоже на IPA (нет Payload/*.app/Info.plist)"})
            if g == "/api/lib/add":
                return self.send(200, {"log": f"Добавлено в библиотеку: {fn}"})
            return self.send(200, {"job": job_start(f"{name}: подготовка…", lambda j: install_bytes(j, q["udid"], data))})
        if g == "/api/lib/install":
            p = lib_path(q["file"])
            return self.send(200, {"job": job_start("Подготовка…", lambda j: install_bytes(j, q["udid"], open(p, "rb").read()))})
        if g == "/api/lib/delete":
            return self.safe(lambda: [os.remove(x) for x in (lib_path(q["file"]), lib_path(q["file"]) + ".json")
                                      if os.path.exists(x)] and {"log": "Удалено из библиотеки"})
        if g == "/api/uninstall": return self.safe(lambda: _uninstall(q["udid"], q["id"]))
        if g == "/api/docs/upload":
            return self.safe(lambda: _docs_put(q["udid"], q["id"], q["path"], self.body()))
        if g == "/api/docs/delete": return self.safe(lambda: _docs_rm(q["udid"], q["id"], q["path"]))
        if g == "/api/backup":
            return self.send(200, {"job": job_start("Бэкап…", lambda j: backup_app(j, q["udid"], q["id"], q.get("name", q["id"])))})
        if g == "/api/backup/import":
            name = safe_name(os.path.basename(unquote(self.headers.get("X-Filename", "backup.averoapp"))))
            with open(os.path.join(BAK, name), "wb") as f:
                f.write(self.body())
            return self.send(200, {"log": "Бэкап добавлен"})
        if g == "/api/backup/restore":
            return self.send(200, {"job": job_start("Восстановление…", lambda j: restore_app(j, q["udid"], q["file"]))})
        if g == "/api/backup/delete":
            return self.safe(lambda: os.remove(lib_path(q["file"], BAK)) or {"log": "Бэкап удалён"})
        if g == "/api/open":
            return self.safe(lambda: open_folder({"lib": LIB, "bak": BAK}.get(q.get("what"), DATA)) or {"log": "ok"})
        if g == "/api/as/login":
            d = json.loads(self.body() or b"{}")
            return self.safe(lambda: as_login(d.get("email", "").strip(), d.get("password", ""), d.get("code", "").strip()))
        if g == "/api/as/logout": return self.safe(as_logout)
        if g == "/api/settings":
            d = json.loads(self.body() or b"{}")
            return self.send(200, settings({"proxy": str(d.get("proxy", "")).strip()}))
        if g == "/api/as/download":
            return self.send(200, {"job": job_start("App Store…", lambda j: as_download(j, q["id"]))})
        if g == "/api/update/apply":
            return self.send(200, {"job": job_start("Проверка обновления…", update_apply)})
        self.send(404, {"error": "not found"})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    if "--selftest" in sys.argv:  # проверка сборки в CI: модули и страница на месте
        import traceback
        try:
            import webview  # noqa: F401
            assert os.path.exists(os.path.join(BASE, "index.html")), "index.html не упакован"
            assert os.path.exists(IPATOOL), "ipatool не упакован"
            msg, code = "selftest ok " + __version__, 0
        except BaseException:
            msg, code = traceback.format_exc(), 1
        with open("selftest.log", "w", encoding="utf-8") as f:
            f.write(msg)
        os._exit(code)
    threading.Timer(5, cleanup_old).start()
    url = f"http://127.0.0.1:{PORT}"
    import time
    for _ in range(20 if "--updated" in sys.argv else 1):  # после обновления ждём, пока старый процесс отпустит порт
        try:
            srv = ThreadingHTTPServer((HOST, PORT), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            break
        except OSError:
            time.sleep(0.5)  # иначе уже запущен — откроем ещё одно окно к нему
    try:
        import webview
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        webview.create_window("Avero", url, width=1000, height=780, min_size=(420, 560))
        webview.start()
    except Exception:  # нет WebView2 — откроем в браузере
        webbrowser.open(url)
        threading.Event().wait()
    os._exit(0)


if __name__ == "__main__":
    main()
