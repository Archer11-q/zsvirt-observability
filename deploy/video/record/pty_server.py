"""把**真实 PTY** 送到浏览器里，供录屏使用。

## 为什么需要它

演示视频要「录屏」，而录屏里最关键的证据是**终端里真实执行的命令与输出**。
我（Agent）没有交互式桌面的控制与采集能力，所以不能"打开一个终端窗口去操作它"。
可行的等价做法是：在 WSL 里开一个**真正的伪终端**跑 bash，把它的输出流送到
浏览器页面，再用 Chrome 的 CDP screencast 把页面录下来 ——
录到的是**真实 PTY 的真实输出**，不是事后重排的回放。

## 为什么不引第三方库

只用标准库：`pty` / `subprocess` / `select` / `http.server`。
理由与项目 `ADR-0003` 一致 —— 这是录制工具，装一次就为了录一次的东西不该引入依赖。
另外本机 WSL 没有免密 sudo，装不了 `ttyd` / `gotty` 这类现成的 web 终端。

## 为什么不用 WebSocket

WebSocket 在标准库里没有。用一个 200ms 的轮询接口就够：终端输出是文本、
量很小，轮询实现简单且**出问题时容易看**（一个 curl 就能看到原始字节流，
而 WebSocket 需要专门的客户端才能调试）。

用法：
    python3 pty_server.py --port 5199 --cwd ~/archer/workspace/zsvirt-observability
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))

#: 提示符刻意做得干净且带产品强调色（青蓝 #0E7C86 对应 256 色的 44）。
#: 用 `--norc` 起 bash，避免加载个人配置 —— 那些配置会往终端里打一堆
#: 与演示无关的东西（git 状态、欢迎语），录进视频里就是噪声。
PROMPT = (
    r"\[\e[38;5;44m\]archer@devbox\[\e[0m\]:"
    r"\[\e[38;5;44m\]\w\[\e[0m\]\$ "
)


class Session:
    """一个持有真实 PTY 的 bash 会话。"""

    def __init__(self, name: str, cwd: str, cols: int = 118, rows: int = 34) -> None:
        self.name = name
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.exit_code: int | None = None

        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        env = dict(os.environ)
        env.update(
            {
                "TERM": "xterm-256color",
                "PS1": PROMPT,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                # 关掉不需要的东西，让录制画面干净、输出稳定
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "SYSTEMD_PAGER": "cat",
                "PYTHONUNBUFFERED": "1",
            }
        )
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get(
            "PATH", ""
        )

        self.proc = subprocess.Popen(  # noqa: S603 - 参数是写死的 bash
            ["bash", "--norc", "-i"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.master = master
        threading.Thread(target=self._pump, daemon=True).start()
        # 让提示符先出一次，页面一打开就有一个干净的起手画面
        time.sleep(0.35)

    def _pump(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.2)
                if not ready:
                    if self.proc.poll() is not None:
                        break
                    continue
                data = os.read(self.master, 65536)
                if not data:
                    break
                with self.lock:
                    self.buffer += data
            except OSError:
                break
        self.exit_code = self.proc.poll()

    def write(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))

    def read_from(self, offset: int) -> tuple[str, int]:
        with self.lock:
            chunk = bytes(self.buffer[offset:])
            size = len(self.buffer)
        return chunk.decode("utf-8", errors="replace"), size

    def interrupt(self) -> None:
        """发 Ctrl-C。演示里"停掉服务"用得上。"""
        os.write(self.master, b"\x03")

    def close(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            os.close(self.master)
        except OSError:
            pass


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()
ARGS: argparse.Namespace


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        pass  # 录制期间不要往 stderr 刷请求日志，免得污染演示输出

    # ---- 小工具 ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "studio.html"), "rb") as handle:
                self._send(200, handle.read(), "text/html; charset=utf-8")
            return

        if path == "/api/out":
            session = SESSIONS.get(params.get("id", ""))
            if session is None:
                self._json({"error": "no such session"}, 404)
                return
            data, size = session.read_from(int(params.get("from", "0")))
            self._json({"data": data, "next": size, "exitCode": session.exit_code})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        body = self._body()

        if path == "/api/session":
            name = str(body.get("name") or "main")
            with SESSIONS_LOCK:
                if name in SESSIONS:
                    self._json({"id": name, "reused": True})
                    return
                SESSIONS[name] = Session(name, ARGS.cwd)
            self._json({"id": name, "reused": False})
            return

        session = SESSIONS.get(str(body.get("id") or ""))
        if session is None:
            self._json({"error": "no such session"}, 404)
            return

        if path == "/api/exec":
            cmd = str(body.get("cmd") or "")
            # 逐字符写入，让浏览器端**看得见命令被敲进去**。
            # 一次性写入会在画面上瞬间出现整行，看起来像回放而不是操作。
            delay = float(body.get("typingDelay") or 0)
            if delay > 0:
                for ch in cmd:
                    session.write(ch)
                    time.sleep(delay)
                session.write("\n")
            else:
                session.write(cmd + "\n")
            self._json({"ok": True})
            return

        if path == "/api/send":
            session.write(str(body.get("text") or ""))
            self._json({"ok": True})
            return

        if path == "/api/interrupt":
            session.interrupt()
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


def main() -> None:
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5199)
    parser.add_argument("--cwd", default=os.path.expanduser("~"))
    ARGS = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    print(f"pty_server 监听 http://127.0.0.1:{ARGS.port}  cwd={ARGS.cwd}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for session in SESSIONS.values():
            session.close()


if __name__ == "__main__":
    main()
