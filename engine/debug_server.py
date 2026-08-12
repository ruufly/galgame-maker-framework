"""引擎调试服务器 (可选): 后台线程 TCP JSON 服务。

供编辑器预览窗口实时调试运行中的游戏:

- 引擎每帧由 frame_hook 统计 FPS, 每 0.5s 向所有客户端推送一次状态::

      {"type": "state", "fps": 60.0, "frame_ms": 16.6,
       "vars": {"main::love": 1, ...}, "label": "game_start", "log": [...]}

- 日志经 log.on_message 实时追加 (随下一次状态推送发出, 增量)
- 支持请求 (JSON 行, 每行一个):
      {"type": "get_var", "name": "love"}     -> {"type": "var", ...}
      {"type": "set_var", "name": "love",
       "value": 2}                            -> {"type": "ok"}
      {"type": "eval", "code": "engine.get_var('love')"}
                                              -> {"type": "eval_result", ...}
      {"type": "set_lang", "lang": "zh-CN"}   -> {"type": "ok"}
- 安全: 仅绑定 127.0.0.1; 只对显式启用的进程生效 (gamelauncher --debug-port)

线程模型: 调试线程与引擎主线程共享 engine 对象, 仅做轻量读取/受控写入,
CPython GIL 下原子性足够, 不加锁 (避免与主循环死锁)。
"""

import json
import socket
import threading
import time

from framework.engine import log


def _to_jsonable(value):
    """变量值 -> JSON 可序列化 (容器截断, 对象转 repr)。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        try:
            return [_to_jsonable(v) for v in value[:50]]
        except Exception:
            return repr(value)
    if isinstance(value, dict):
        try:
            return {str(k): _to_jsonable(v)
                    for k, v in list(value.items())[:50]}
        except Exception:
            return repr(value)
    return repr(value)[:200]


def _coerce(value):
    """调试 set_var 值宽松转换: 数字/布尔/字符串。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (int, float)):
        return value
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class DebugServer:
    """每帧推送状态 + 响应查询。start() 后即生效, 进程结束自动清理。"""

    PUSH_INTERVAL = 0.5   # 秒

    def __init__(self, engine, port: int):
        self.engine = engine
        self.port = port
        self._clients: list = []
        self._fps = 0.0
        self._frame_ms = 0.0
        self._log_pending: list = []   # 待推送日志 (增量)
        self._last_push = 0.0
        self._stop = False

    # ---- 启动 ---------------------------------------------------------
    @classmethod
    def start(cls, engine, port: int) -> "DebugServer":
        srv = cls(engine, port)
        srv._install()
        threading.Thread(target=srv._serve, daemon=True,
                         name="gal-debug-server").start()
        log.i("log.debug_server_started", port=port)
        return srv

    def _install(self) -> None:
        self.engine.register_frame_hook(self._on_frame)
        log.on_message(self._on_log)

    # ---- 引擎侧回调 (主线程) -----------------------------------------
    def _on_frame(self, dt: float) -> None:
        now = time.time()
        if dt > 0:
            inst = 1.0 / dt
            self._fps = self._fps * 0.9 + inst * 0.1
            self._frame_ms = self._frame_ms * 0.9 + dt * 1000.0 * 0.1
        if now - self._last_push >= self.PUSH_INTERVAL:
            self._last_push = now
            self._push_state()

    def _on_log(self, level: str, line: str) -> None:
        self._log_pending.append({"level": level, "line": line})
        if len(self._log_pending) > 500:
            del self._log_pending[:100]

    def _push_state(self) -> None:
        if not self._clients:
            self._log_pending.clear()
            return
        state = {
            "type": "state",
            "fps": round(self._fps, 1),
            "frame_ms": round(self._frame_ms, 2),
            "vars": self._collect_vars(),
            "label": self._current_label(),
            "log": self._log_pending,
        }
        self._log_pending = []
        self._broadcast(state)

    def _collect_vars(self) -> dict:
        out = {}
        try:
            for k, v in list(self.engine.runtime.vars.items())[:300]:
                out[str(k)] = _to_jsonable(v)
        except Exception:
            pass
        return out

    def _current_label(self) -> str:
        try:
            return getattr(self.engine.runtime, "current_label", "") or ""
        except Exception:
            return ""

    # ---- 网络 ---------------------------------------------------------
    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.port))
            srv.listen(8)
            srv.settimeout(0.5)
        except OSError:
            return
        while not self._stop:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.5)
            self._clients.append(conn)
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True, name="gal-debug-client").start()
        for c in self._clients:
            try:
                c.close()
            except Exception:
                pass
        try:
            srv.close()
        except Exception:
            pass

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        while not self._stop:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                raw = raw.strip()
                if raw:
                    self._dispatch(conn, raw)
        if conn in self._clients:
            self._clients.remove(conn)
        try:
            conn.close()
        except Exception:
            pass

    def _dispatch(self, conn: socket.socket, raw: bytes) -> None:
        try:
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        rtype = req.get("type")
        try:
            if rtype == "get_var":
                name = req.get("name", "")
                val = self.engine.get_var(name) if name else None
                self._send(conn, {"type": "var", "name": name,
                                  "value": _to_jsonable(val)})
            elif rtype == "set_var":
                name = req.get("name", "")
                self.engine.set_var(name, _coerce(req.get("value")))
                self._send(conn, {"type": "ok"})
            elif rtype == "eval":
                code = req.get("code", "")
                ns = {"engine": self.engine,
                      "runtime": self.engine.runtime,
                      "display": self.engine.display}
                result = eval(code, {"__builtins__": {}}, ns)  # noqa: S307
                self._send(conn, {"type": "eval_result",
                                  "value": _to_jsonable(result)})
            elif rtype == "set_lang":
                lang = req.get("lang", "")
                if lang:
                    self.engine.i18n.set_lang(lang)
                self._send(conn, {"type": "ok"})
        except Exception as exc:  # noqa: BLE001 - 调试请求异常仅回显
            self._send(conn, {"type": "error",
                              "message": "%s: %s"
                              % (type(exc).__name__, exc)})

    def _broadcast(self, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        dead = []
        for c in self._clients:
            try:
                c.sendall(data)
            except OSError:
                dead.append(c)
        for c in dead:
            if c in self._clients:
                self._clients.remove(c)

    @staticmethod
    def _send(conn: socket.socket, payload: dict) -> None:
        try:
            conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n")
                         .encode("utf-8"))
        except OSError:
            pass

    def stop(self) -> None:
        self._stop = True
