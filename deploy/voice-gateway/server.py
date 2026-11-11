"""Restricted device proxy. No session cookies, admin routes or cached replies."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re

ALLOWED = re.compile(r"^/api/v1/voice/device/(?:config/|heartbeat/|standby/|tasks/(?:[0-9]+/(?:(?:rename|delete)/)?)?|task-selection/|task-home/|task-menu/[0-9]+/|turns/|turns/[0-9a-f-]{36}/(?:(?:retry|cancel)/|pages/(?:answer|transcript)/(?:batch/)?[0-9]+/)?)$")
MAX_BODY = 3 * 1024 * 1024


class BodyTooLarge(ValueError):
    pass


def read_body(stream, length_header, encoding):
    """Cloudflared may rechunk even an empty request; decode with strict bounds."""
    if encoding:
        if length_header is not None or encoding.strip().lower() != "chunked":
            raise ValueError("Ambiguous request framing")
        body = bytearray()
        for _ in range(4096):
            line = stream.readline(129)
            if not line.endswith(b"\r\n") or len(line) > 128:
                raise ValueError("Invalid chunk header")
            token = line[:-2].split(b";", 1)[0]
            if not re.fullmatch(rb"[0-9a-fA-F]{1,8}", token):
                raise ValueError("Invalid chunk length")
            size = int(token, 16)
            if not size:
                trailer_bytes = 0
                while True:
                    trailer = stream.readline(1025)
                    trailer_bytes += len(trailer)
                    if not trailer.endswith(b"\r\n") or len(trailer) > 1024 or trailer_bytes > 8192:
                        raise ValueError("Invalid chunk trailer")
                    if trailer == b"\r\n":
                        return bytes(body)
            if len(body) + size > MAX_BODY:
                raise BodyTooLarge()
            chunk = stream.read(size)
            if len(chunk) != size or stream.read(2) != b"\r\n":
                raise ValueError("Incomplete chunk")
            body.extend(chunk)
        raise ValueError("Too many chunks")
    length = int(length_header or "0")
    if length < 0:
        raise ValueError("Invalid length")
    if length > MAX_BODY:
        raise BodyTooLarge()
    body = stream.read(length) if length else b""
    if len(body) != length:
        raise ValueError("Incomplete body")
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args):
        pass

    def proxy(self):
        if not ALLOWED.fullmatch(self.path):
            self.send_error(404); return
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 256:
            # A rejected request may still have an unread body. Never reuse its
            # connection and interpret those bytes as another HTTP request.
            self.close_connection = True
            self.send_response(401); self.send_header("Content-Length", "0")
            self.send_header("Connection", "close"); self.end_headers(); return
        self.connection.settimeout(30)
        conn = http.client.HTTPConnection("web", 8000, timeout=30)
        try:
            try:
                body = read_body(self.rfile, self.headers.get("Content-Length"), self.headers.get("Transfer-Encoding"))
            except BodyTooLarge:
                self.send_error(413); return
            except ValueError:
                self.send_error(400); return
            conn.request(self.command, self.path, body=body, headers={
                "Host": "localhost", "Authorization": authorization,
                "Content-Type": self.headers.get("Content-Type", "application/octet-stream"),
                "Connection": "close",
            })
            upstream = conn.getresponse()
            data = upstream.read(1024 * 1024)
            self.send_response(upstream.status)
            for key in ("Content-Type", "X-Content-SHA256", "X-Page-Count", "X-Frame-Count", "X-Frame-Format", "X-Menu-Count", "X-Menu-Conversation"):
                if upstream.getheader(key): self.send_header(key, upstream.getheader(key))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        except (TimeoutError, OSError, http.client.HTTPException):
            self.send_error(502)
        finally:
            conn.close()

    do_GET = proxy
    do_POST = proxy


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.daemon_threads = True
    server.serve_forever()
