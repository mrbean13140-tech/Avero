"""Avero — установка/удаление IPA на iPhone через веб-интерфейс (аналог iMazing, раздел «Приложения»)."""
import asyncio, json, os, socket, sys, threading, uuid, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

if sys.stdout is None or sys.stderr is None:  # exe без консоли: прогресс-бары и логи пишем в никуда
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3 import exceptions as E

__version__ = "dev"
PORT = int(os.environ.get("PORT", 8765))
HOST = os.environ.get("HOST", "0.0.0.0")
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APPLE_DEVICES = "https://apps.microsoft.com/detail/9NP83LWLPZ9K"
JOBS = {}

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def call(coro, timeout=300):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


def human(e: Exception) -> str:
    if isinstance(e, (ConnectionRefusedError, FileNotFoundError)) or "27015" in str(e) or "usbmuxd" in str(e).lower():
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
    return f"{type(e).__name__}: {e}"[:600]


async def lockdown(udid):
    return await create_using_usbmux(serial=udid, pair_timeout=60)


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
            data = await ip.get_apps(application_type="User")
    out = [{"id": b, "name": a.get("CFBundleDisplayName") or a.get("CFBundleName") or b,
            "ver": a.get("CFBundleShortVersionString", "")} for b, a in data.items()]
    return sorted(out, key=lambda x: x["name"].lower())


async def _install(job, udid, data):
    def progress(p, *_):
        JOBS[job]["log"] = f"Установка… {p}%"
    try:
        async with await lockdown(udid) as ld:
            async with InstallationProxyService(lockdown=ld) as ip:
                JOBS[job]["log"] = "Загрузка на устройство…"
                await ip.install_from_bytes(data, handler=progress)
        JOBS[job].update(state="ok", log="Установлено")
    except Exception as e:
        JOBS[job].update(state="error", log=human(e))


async def _uninstall(udid, bid):
    async with await lockdown(udid) as ld:
        async with InstallationProxyService(lockdown=ld) as ip:
            await ip.uninstall(bid)
    return {"log": "Удалено"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def safe(self, coro):
        try: self.send(200, call(coro))
        except Exception as e: self.send(500, {"error": human(e)})

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/":
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self.send(200, f.read().replace(b"{{VERSION}}", __version__.encode()), "text/html; charset=utf-8")
        if u.path == "/api/info": return self.send(200, {"phone": f"http://{lan_ip()}:{PORT}"})
        if u.path == "/api/devices": return self.safe(_devices())
        if u.path == "/api/apps": return self.safe(_apps(q["udid"][0]))
        if u.path == "/api/job": return self.send(200, JOBS.get(q["id"][0], {"state": "error", "log": "нет задачи"}))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/api/install":
            name = os.path.basename(unquote(self.headers.get("X-Filename", "app.ipa")))
            if not name.lower().endswith(".ipa"):
                return self.send(400, {"error": "Нужен файл .ipa"})
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            job = uuid.uuid4().hex[:8]
            JOBS[job] = {"state": "running", "log": f"{name}: подготовка…"}
            asyncio.run_coroutine_threadsafe(_install(job, q["udid"][0], data), LOOP)
            return self.send(200, {"job": job})
        if u.path == "/api/uninstall":
            return self.safe(_uninstall(q["udid"][0], q["id"][0]))
        self.send(404, {"error": "not found"})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    if "--selftest" in sys.argv:  # проверка сборки в CI: модули и страница на месте
        import webview  # noqa: F401
        assert os.path.exists(os.path.join(BASE, "index.html"))
        sys.exit(0)
    url = f"http://127.0.0.1:{PORT}"
    try:
        srv = ThreadingHTTPServer((HOST, PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError:
        pass  # уже запущен — просто открываем ещё одно окно к нему
    try:
        import webview
        webview.create_window("Avero", url, width=980, height=760, min_size=(420, 560))
        webview.start()
    except Exception:  # нет WebView2 — откроем в браузере
        webbrowser.open(url)
        threading.Event().wait()


if __name__ == "__main__":
    main()
