"""A small Python kernel that runs inside the Groundbox sandbox.

This file is uploaded to the sandbox as it is and needs only the standard library
(Python 3.8 or newer). It has two modes:

    python3 _kernel.py serve SOCKET   keep one Python namespace alive and run code sent to SOCKET
    python3 _kernel.py run SOCKET     read a request (JSON) on stdin, send it to the server and
                                      print the answer (JSON) on stdout

The executor starts `serve` once as a background command, then runs `run` for each code
action with Groundbox's exec call. Variables, imports and functions stay in the server's
namespace between actions, the way a Jupyter kernel keeps them for the built-in executors.
"""

import ast
import base64
import io
import json
import linecache
import os
import signal
import site
import socket
import struct
import sys
import tempfile
import time
import traceback

FINAL_ANSWER_EXCEPTION = "FinalAnswerException"


class ExecutionTimeout(BaseException):
    """Raised inside running code when it runs past its time limit."""


def _send(conn, payload):
    data = json.dumps(payload).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _recv(conn):
    header = _recv_exact(conn, 4)
    if header is None:
        return None
    (size,) = struct.unpack(">I", header)
    body = _recv_exact(conn, size)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _recv_exact(conn, size):
    chunks = []
    while size > 0:
        chunk = conn.recv(min(size, 1 << 20))
        if not chunk:
            return None
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _image_png(value):
    """PNG bytes for a PIL image, or None for anything else (PIL is not imported here)."""
    module = type(value).__module__ or ""
    if not module.startswith("PIL.") or not hasattr(value, "save"):
        return None
    buffer = io.BytesIO()
    value.save(buffer, format="PNG")
    return buffer.getvalue()


class Kernel:
    def __init__(self):
        self.namespace = {"__name__": "__main__", "__builtins__": __builtins__}
        self.count = 0

    def _refresh_paths(self):
        # Packages installed with `pip install --user` after this process started live in a
        # folder that was not on sys.path at start-up if it did not exist yet.
        try:
            user_site = site.getusersitepackages()
            if os.path.isdir(user_site) and user_site not in sys.path:
                site.addsitedir(user_site)
        except Exception:
            pass
        try:
            import importlib

            importlib.invalidate_caches()
        except Exception:
            pass

    def _compile(self, code, filename):
        tree = ast.parse(code, filename=filename, mode="exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(body=tree.body.pop().value)
        body = compile(tree, filename, "exec")
        expr = compile(last, filename, "eval") if last is not None else None
        return body, expr

    def execute(self, code, timeout_s=None):
        self.count += 1
        filename = "<code-%d>" % self.count
        linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
        self._refresh_paths()

        # Capture output at the file-descriptor level, so subprocesses and C extensions
        # are captured along with print().
        sys.stdout.flush()
        sys.stderr.flush()
        capture = tempfile.TemporaryFile()
        saved_out, saved_err = os.dup(1), os.dup(2)
        os.dup2(capture.fileno(), 1)
        os.dup2(capture.fileno(), 2)

        result = {"output": None, "output_type": None, "final_answer": None, "error": None}
        previous_handler = None
        if timeout_s:
            def on_alarm(signum, frame):
                raise ExecutionTimeout("Code execution ran past %s seconds" % timeout_s)

            previous_handler = signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, float(timeout_s))
        try:
            body, expr = self._compile(code, filename)
            exec(body, self.namespace, self.namespace)
            if expr is not None:
                value = eval(expr, self.namespace, self.namespace)
                if value is not None:
                    png = _image_png(value)
                    if png is not None:
                        result["output"] = base64.b64encode(png).decode("ascii")
                        result["output_type"] = "image/png"
                    else:
                        result["output"] = repr(value)
                        result["output_type"] = "text/plain"
        except BaseException as error:  # noqa: B036 - code may raise anything, SystemExit included
            if type(error).__name__ == FINAL_ANSWER_EXCEPTION:
                value = getattr(error, "value", None)
                result["final_answer"] = value if isinstance(value, str) else str(error)
            else:
                result["error"] = {
                    "name": type(error).__name__,
                    "value": str(error),
                    "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                }
        finally:
            if timeout_s:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler or signal.SIG_DFL)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(saved_out)
            os.close(saved_err)
            capture.seek(0)
            result["logs"] = capture.read().decode("utf-8", errors="replace")
            capture.close()
        return result


def serve(path):
    # Line-buffered, so print() output and subprocess output land in the order they happen.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(4)
    kernel = Kernel()
    while True:
        conn, _ = server.accept()
        try:
            request = _recv(conn)
            if request is None:
                continue
            if request.get("ping"):
                _send(conn, {"pong": True, "python": sys.version.split()[0], "pid": os.getpid()})
                continue
            _send(conn, kernel.execute(request["code"], request.get("timeout_s")))
        except Exception as error:  # keep serving whatever one connection did
            try:
                _send(conn, {"error": {"name": type(error).__name__, "value": str(error), "traceback": ""}})
            except Exception:
                pass
        finally:
            conn.close()


def run(path, wait_s=30.0):
    request = json.loads(sys.stdin.read())
    start = time.monotonic()
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(path)
            break
        except (FileNotFoundError, ConnectionRefusedError) as error:
            conn.close()
            # No socket yet: the server may still be starting. A socket nobody listens on:
            # the server has ended, so give up after a moment.
            limit = wait_s if isinstance(error, FileNotFoundError) else 1.0
            if time.monotonic() - start >= limit:
                print(json.dumps({"kernel_unavailable": True}))
                return
            time.sleep(0.05)
    with conn:
        _send(conn, request)
        answer = _recv(conn)
    print(json.dumps(answer if answer is not None else {"kernel_unavailable": True}))


if __name__ == "__main__":
    mode, socket_path = sys.argv[1], sys.argv[2]
    if mode == "serve":
        serve(socket_path)
    elif mode == "run":
        run(socket_path)
    else:
        raise SystemExit("usage: _kernel.py serve|run SOCKET")
