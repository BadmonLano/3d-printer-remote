"""Fake Creality OS printer for offline tests: HTTP upload (/upload/<name>) + websocket (port 9999 protocol).

Records every upload and every websocket message so tests can assert exactly what was sent.
Stdlib only. Run standalone: python fake_printer.py  (prints the ports).
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakePrinter:
    def __init__(self):
        self.uploads: dict[str, bytes] = {}
        self.ws_messages: list = []
        self.status = {"state": 0, "deviceState": 0, "printFileName": "", "printProgress": 0,
                       "nozzleTemp": "25.0", "targetNozzleTemp": 0, "bedTemp0": "24.0", "targetBedTemp0": 0,
                       "model": "F003", "hostname": "fake-cr10se", "err": {"errcode": 0, "key": 0},
                       "printJobTime": 0, "printLeftTime": 0, "layer": 0, "TotalLayer": 0,
                       "modelFanPct": 0, "auxiliaryFanPct": 0, "caseFanPct": 0,
                       "curPosition": "X:0.00 Y:0.00 Z:0.00"}
        self.push_interval = 0.3  # live pushes like Creality OS
        self._http = None
        self._ws_sock = None
        self.http_port = self.ws_port = 0

    # ---------------- HTTP
    def _handler(self):
        fp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fake creality")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                if not self.path.startswith("/upload/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                ctype = self.headers.get("Content-Type", "")
                boundary = ctype.split("boundary=")[-1].encode()
                part = body.split(b"--" + boundary)[1]
                content = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
                fp.uploads[self.path[len("/upload/"):]] = content
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":200,"message":"OK"}')
        return H

    # ---------------- WS
    def _ws_client(self, conn: socket.socket):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            key = [l.split(b":", 1)[1].strip() for l in data.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
            self._send(conn, json.dumps({"ModeCode": "heart_beat"}))
            self._send(conn, json.dumps(self.status))
            stop = threading.Event()

            def pusher():
                while not stop.wait(self.push_interval):
                    try:
                        self._send(conn, json.dumps(self.status))
                    except OSError:
                        return
            threading.Thread(target=pusher, daemon=True).start()
            conn.settimeout(10)
            while True:
                msg = self._recv(conn)
                if msg is None:
                    break
                if msg == "ok":
                    continue
                try:
                    d = json.loads(msg)
                except ValueError:
                    continue
                self.ws_messages.append(d)
                params = d.get("params", {})
                if d.get("method") == "get" and "reqGcodeFile" in params:
                    # format seen on a real CR-10 SE (DWIN V1.1.0.28): one string, ';'-separated, ':'-separated fields
                    info = "".join(f"/usr/data/printer_data/gcodes:{n}:{len(b)}:0.000000:1792126009:0:"
                                   f"/tmp/creality/local_gcode/humbnail/{n[:-6]}.png;" for n, b in self.uploads.items())
                    self._send(conn, json.dumps({"retGcodeFileInfo": {"totalNum": len(self.uploads), "fileInfo": info}}))
                    continue
                if d.get("method") == "get" and "reqHistory" in params:
                    self._send(conn, json.dumps({"historyList": [{"filename": "old.gcode", "printfinish": 1}]}))
                    continue
                if "opGcodeFile" in params:
                    self.status.update(state=1, deviceState=1, printFileName=params["opGcodeFile"].split("/")[-1])
                if params.get("pause") == 1:
                    self.status.update(state=5)
                if params.get("stop") == 1:
                    self.status.update(state=4, deviceState=0)
                self._send(conn, json.dumps(self.status))
        except (OSError, IndexError):
            pass
        finally:
            try:
                stop.set()
            except NameError:
                pass
            conn.close()

    @staticmethod
    def _send(conn, text):
        b = text.encode()
        hdr = bytes([0x81])
        hdr += bytes([len(b)]) if len(b) < 126 else bytes([126]) + struct.pack(">H", len(b))
        conn.sendall(hdr + b)

    @staticmethod
    def _recv(conn):
        def n(k):
            buf = b""
            while len(buf) < k:
                c = conn.recv(k - len(buf))
                if not c:
                    raise OSError
                buf += c
            return buf
        b1, b2 = n(2)
        if b1 & 0x0F == 8:
            return None
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", n(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", n(8))[0]
        mask = n(4)
        data = n(ln)
        return bytes(x ^ mask[i % 4] for i, x in enumerate(data)).decode()

    def _ws_loop(self):
        while True:
            try:
                c, _ = self._ws_sock.accept()
            except OSError:
                return
            threading.Thread(target=self._ws_client, args=(c,), daemon=True).start()

    def start(self):
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.http_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self._ws_sock = socket.socket()
        self._ws_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ws_sock.bind(("127.0.0.1", 0))
        self._ws_sock.listen(5)
        self.ws_port = self._ws_sock.getsockname()[1]
        threading.Thread(target=self._ws_loop, daemon=True).start()
        return self

    def stop(self):
        self._http.shutdown()
        self._http.server_close()
        self._ws_sock.close()


if __name__ == "__main__":
    import time
    p = FakePrinter().start()
    print(json.dumps({"http_port": p.http_port, "ws_port": p.ws_port}))
    while True:
        time.sleep(1)
