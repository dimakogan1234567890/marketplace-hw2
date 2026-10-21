"""A complete HTTP scenario using only the Python standard library."""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = os.getenv("API_URL", "http://127.0.0.1:8080")


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(BASE_URL + path, data=data, headers=headers, method=method), timeout=20) as response:
        print(method, path, response.status, "request_id=" + response.headers["X-Request-Id"])
        return json.load(response)


def account(role, suffix):
    name = role.lower() + "_" + suffix
    password = "Demo-password-123"
    call("POST", "/auth/register", {"username": name, "password": password, "role": role})
    return call("POST", "/auth/login", {"username": name, "password": password})["access_token"]


def main():
    suffix = uuid4().hex[:12]
    seller, buyer = account("SELLER", suffix), account("USER", suffix)
    product = call("POST", "/products", {"name": "Keyboard", "price": 100, "stock": 10,
                   "category": "tech", "status": "ACTIVE"}, seller)
    now, code = datetime.now(timezone.utc), "DEMO_" + suffix.upper()
    call("POST", "/promo-codes", {"code": code, "discount_type": "PERCENTAGE", "discount_value": 10,
         "min_order_amount": 200, "max_uses": 10, "active": True,
         "valid_from": (now - timedelta(days=1)).isoformat(), "valid_until": (now + timedelta(days=1)).isoformat()}, seller)
    body = {"items": [{"product_id": product["id"], "quantity": 3}], "promo_code": code}
    created = call("POST", "/orders", body, buyer)
    print("Created: total=", created["total_amount"], "expected=270; order_id=", created["id"])
    path = "/orders/" + created["id"]
    changed = call("PUT", path, {"items": [{"product_id": product["id"], "quantity": 1}]}, buyer)
    print("Updated: total=", changed["total_amount"], "expected=100; promo_code_id=", changed["promo_code_id"])
    canceled = call("POST", path + "/cancel", token=buyer)
    final = call("GET", "/products/" + product["id"], token=buyer)
    print("Final:", canceled["status"], "stock=", final["stock"], "expected=10")
    print("Check SQL: product_id=", product["id"], "promo_code=", code)


if __name__ == "__main__":
    main()
