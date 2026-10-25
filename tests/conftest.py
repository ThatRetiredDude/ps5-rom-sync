import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer


@pytest.fixture
def ftp_server(tmp_path):
    root = tmp_path / "ps5"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_anonymous(str(root), perm="elradfmwMT")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    t.start()
    yield {"host": "127.0.0.1", "port": port, "user": "anonymous", "password": "", "root": root}
    server.close_all()
