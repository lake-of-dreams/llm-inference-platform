import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def http_server():
    """Starts a real HTTP server on a free local port.

    The test passes a handler function that receives the request handler and
    writes the response. The fixture returns a function that starts the server
    and gives back its base URL.
    """
    servers = []

    def start(handle) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                handle(self)

            def do_POST(self):
                handle(self)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for srv in servers:
        srv.shutdown()
