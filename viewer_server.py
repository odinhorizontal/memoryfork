"""Offline replay viewer and project-scoped background service controller."""

import argparse
import errno
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import selectors
import socket
import subprocess
import sys
import time
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

import replay_model


PROJECT = Path(__file__).resolve().parent
STATE = PROJECT / ".viewer"
HOST = "127.0.0.1"
PROJECT_ID = hashlib.sha256(os.fsencode(PROJECT)).hexdigest()
ASSETS = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
          "/app.js": ("app.js", "text/javascript")}


class Viewer(ThreadingHTTPServer):
    # Finish an in-flight comparison before releasing the service state.
    daemon_threads = False

    def server_bind(self):
        # Windows SO_REUSEADDR could share a port with another checkout.
        if sys.platform == "win32":
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, port, root, token, stop_signal):
        super().__init__((HOST, port), Handler)
        self.root = root
        self.token = token
        self.stop_signal = stop_signal
        self.job = Lock()
        self.url = f"http://{HOST}:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *args):
        pass

    def send_content(self, status, data, content_type="application/json"):
        if content_type == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        server = self.server
        if self.headers.get("Host") != f"{HOST}:{server.server_port}":
            return self.send_content(403, {"error": "仅允许本机访问"})
        if self.headers.get("Origin", server.url) != server.url:
            return self.send_content(403, {"error": "请求来源不匹配"})
        path = urlsplit(self.path).path
        try:
            if path in ("/control/status", "/control/stop"):
                return self.control(path)
            if self.command == "GET" and path in ASSETS:
                name, mime = ASSETS[path]
                return self.send_content(200, (PROJECT / "viewer" / name).read_bytes(), mime)
            if path == "/api/runs":
                if self.command == "GET":
                    return self.send_content(200, {"runs": replay_model.list_runs(server.root)})
                with server.job:
                    run = replay_model.create_run(server.root)
                return self.send_content(201, run)
            match = re.fullmatch(r"/api/runs/([a-zA-Z0-9_-]+)(?:/([AB])/compare)?", path)
            if match:
                run_id, env = match.groups()
                if self.command == "GET" and env is None:
                    return self.send_content(200, replay_model.load_run(server.root, run_id))
                if self.command == "POST" and env:
                    with server.job:
                        result = replay_model.compare(server.root, run_id, env)
                    return self.send_content(200, result)
            self.send_content(404, {"error": "未找到此资源"})
        except (FileNotFoundError, KeyError):
            self.send_content(404, {"error": "找不到运行或证据文件"})
        except ValueError as exc:
            self.send_content(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            logging.exception("Replay request failed")
            self.send_content(500, {"error": "回放失败，请检查服务日志和冻结库校验值"})

    def control(self, path):
        server = self.server
        if not hmac.compare_digest(self.headers.get("Authorization", ""),
                                   "Bearer " + server.token):
            return self.send_content(403, {"error": "服务令牌不匹配"})
        if path == "/control/status" and self.command == "GET":
            return self.send_content(200, {"project": PROJECT_ID})
        if path == "/control/stop" and self.command == "POST":
            self.send_content(200, {"stopping": True})
            server.stop_signal.send(b"x")
            return
        self.send_content(405, {"error": "请求方法不匹配"})


def exclusive_service(lock_file):
    """Kernel releases this lock even if the service process crashes."""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def serve():
    state_path = STATE / "service.json"
    with (STATE / "service.lock").open("a+b") as lock:
        try:
            exclusive_service(lock)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                # Another start may still be building the first experiment.
                raise SystemExit(75)
            raise
        root = STATE / "runs"
        if not replay_model.list_runs(root):
            replay_model.create_run(root)
        token = secrets.token_hex(32)
        read_signal, write_signal = socket.socketpair()
        with closing(read_signal), closing(write_signal):
            try:
                server = Viewer(8765, root, token, write_signal)
            except OSError:
                server = Viewer(0, root, token, write_signal)
            with server, selectors.DefaultSelector() as selector:
                selector.register(server, selectors.EVENT_READ)
                selector.register(read_signal, selectors.EVENT_READ)
                temp = STATE / "service.tmp"
                temp.write_text(json.dumps(dict(url=server.url, port=server.server_port,
                                                token=token)), encoding="utf-8")
                temp.replace(state_path)
                try:
                    while True:
                        # Block on a request or a shutdown signal, with no idle timer.
                        ready = selector.select()
                        if any(key.fileobj is read_signal for key, mask in ready):
                            break
                        server.handle_request()
                finally:
                    server.server_close()
                    state_path.unlink()


def control(state, action):
    with closing(HTTPConnection(HOST, state["port"], timeout=2)) as conn:
        conn.request("GET" if action == "status" else "POST", "/control/" + action,
                     headers={"Authorization": "Bearer " + state["token"]})
        response = conn.getresponse()
        return json.loads(response.read()) if response.status == 200 else {}


def running():
    try:
        state = json.loads((STATE / "service.json").read_text(encoding="utf-8"))
        if control(state, "status").get("project") == PROJECT_ID:
            return state
    except (OSError, ValueError, KeyError, HTTPException):
        pass
    return None


def start():
    state = running()
    if state:
        print(state["url"])
        return
    STATE.mkdir(exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with (STATE / "service.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"],
                                 cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=flags, start_new_session=sys.platform != "win32")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = running()
        if state:
            print(state["url"])
            return
        if child.poll() not in (None, 75):
            break
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
        child.wait()
    raise SystemExit("Viewer startup failed. See .viewer/service.log.")


def stop():
    state = running()
    if state:
        control(state, "stop")
        path = STATE / "service.json"
        for _ in range(1200):
            if not path.exists() or not running_same_instance(path, state):
                return
            time.sleep(0.1)
        raise SystemExit("Viewer is still finishing its current request.")


def running_same_instance(path, state):
    try:
        return json.loads(path.read_text(encoding="utf-8")) == state
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "serve", "stop"])
    command = parser.parse_args().command
    {"start": start, "serve": serve, "stop": stop}[command]()
