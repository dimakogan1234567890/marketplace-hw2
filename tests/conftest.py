import os
import socket
import threading
import time
from uuid import UUID
import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest
import uvicorn

TEST_URL = os.environ["TEST_DATABASE_URL"]
assert conninfo_to_dict(TEST_URL)["dbname"].endswith("_test"), "Use a dedicated database ending in _test"
os.environ["DATABASE_URL"] = TEST_URL
os.environ["ORDER_INTERVAL_MINUTES"] = "1"


@pytest.fixture(scope="session")
def client():
    from app.main import app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "Uvicorn did not start"
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as connection:
        yield connection
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()


def sql(statement, params=()):
    with psycopg.connect(TEST_URL, autocommit=True) as db:
        cursor = db.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


class API:
    def __init__(self, client):
        self.client = client

    def call(self, method, path, body=None, token=None, **kwargs):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        result = self.client.request(method, path, json=body, headers=headers, **kwargs)
        UUID(result.headers["X-Request-Id"])
        return result

    def ok(self, status, method, path, body=None, token=None):
        result = self.call(method, path, body, token)
        assert result.status_code == status, result.text
        return result.json() if result.content else None

    def account(self, name, role="USER"):
        self.ok(201, "POST", "/auth/register", {"username": name, "password": "Password-123", "role": role})
        return self.ok(200, "POST", "/auth/login", {"username": name, "password": "Password-123"})["access_token"]

    def product(self, stock=10, price=100):
        body = {"name": "Keyboard", "price": price, "stock": stock, "category": "tech", "status": "ACTIVE"}
        return self.ok(201, "POST", "/products", body, self.seller)["id"]


@pytest.fixture
def api(client):
    from app.auth import bootstrap_admin
    sql("TRUNCATE refresh_tokens,user_operations,order_items,orders,promo_codes,products,users CASCADE")
    bootstrap_admin()
    api = API(client)
    api.seller = api.account("seller", "SELLER")
    api.user = api.account("buyer")
    api.admin = api.ok(200, "POST", "/auth/login", {"username": "admin", "password": "Admin-demo-password-123"})["access_token"]
    return api
