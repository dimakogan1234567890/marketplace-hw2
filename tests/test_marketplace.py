from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import json
import logging
from threading import Barrier
from uuid import uuid4
import pytest
from conftest import sql


def error(result, status, code):
    assert result.status_code == status, result.text
    assert result.json()["error_code"] == code


def order(product, quantity=1, promo=None):
    body = {"items": [{"product_id": product, "quantity": quantity}]}
    if promo:
        body["promo_code"] = promo
    return body


def stock(product):
    return sql("SELECT stock FROM products WHERE id=%s", (product,))[0][0]


def count(table):
    return sql(f"SELECT count(*) FROM {table}")[0][0]


def age():
    sql("UPDATE user_operations SET created_at=created_at-interval '2 minutes'")


def promo(api, code="SAVE10", minimum=0, uses=1, **changes):
    from app.auth import now
    body = {"code": code, "discount_type": "PERCENTAGE", "discount_value": 10,
            "min_order_amount": minimum, "max_uses": uses, "active": True,
            "valid_from": (now() - timedelta(days=1)).isoformat(), "valid_until": (now() + timedelta(days=1)).isoformat()}
    return api.ok(201, "POST", "/promo-codes", {**body, **changes}, api.seller)


def race(first, second):
    barrier = Barrier(2)
    def run(fn):
        barrier.wait(timeout=5)
        return fn()
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run, first), pool.submit(run, second)
        return [a.result(), b.result()]


def test_crud_filters_and_ownership(api):
    product = api.product()
    other = api.account("other_seller", "SELLER")
    error(api.call("DELETE", f"/products/{product}", token=other), 403, "ACCESS_DENIED")
    error(api.call("DELETE", f"/products/{product}", token=api.user), 403, "ACCESS_DENIED")
    page = api.ok(200, "GET", "/products?status=ACTIVE&category=tech&size=1&page=0", token=api.user)
    assert page["totalElements"] == 1 and page["size"] == 1
    assert api.ok(200, "GET", "/products?category=Tech", token=api.user)["totalElements"] == 0
    api.ok(204, "DELETE", f"/products/{product}", token=api.seller)
    old = api.ok(200, "GET", f"/products/{product}", token=api.user)
    api.ok(204, "DELETE", f"/products/{product}", token=api.seller)
    assert api.ok(200, "GET", f"/products/{product}", token=api.user) == old
    assert old["status"] == "ARCHIVED" and count("products") == 1


def test_contract_validation_before_writes(api):
    valid = {"name": "A", "price": 1, "stock": 1, "category": "x", "status": "ACTIVE"}
    for change in [{"price": 0}, {"price": 1.001}, {"price": "1.00"}, {"price": True}, {"stock": 1.0},
                   {"stock": -1}, {"name": ""}, {"name": "a" * 256}, {"description": "x" * 4001},
                   {"category": ""}, {"status": "BAD"}, {"unknown": 1}]:
        result = api.call("POST", "/products", {**valid, **change}, api.seller)
        error(result, 400, "VALIDATION_ERROR")
        assert result.json()["details"]["fields"]
    for path in ["/products/no-uuid", "/products?page=-1", "/products?size=101", "/products?status=BAD"]:
        error(api.call("GET", path, token=api.user), 400, "VALIDATION_ERROR")
    error(api.call("POST", "/products", None, api.seller), 400, "VALIDATION_ERROR")
    assert count("products") == 0


def test_atomic_create_snapshot_cancel(api):
    product = api.product()
    created = api.ok(201, "POST", "/orders", order(product, 3), api.user)
    assert stock(product) == 7 and created["total_amount"] == 300
    api.ok(200, "PUT", f"/products/{product}", {"name": "A", "price": 200, "stock": 7, "category": "tech", "status": "ACTIVE"}, api.seller)
    path = f"/orders/{created['id']}"
    assert api.ok(200, "GET", path, token=api.user)["items"][0]["price_at_order"] == 100
    api.ok(200, "POST", path + "/cancel", token=api.user)
    error(api.call("POST", path + "/cancel", token=api.user), 409, "INVALID_STATE_TRANSITION")
    assert stock(product) == 10


def test_create_check_order_and_late_rollback(api):
    product = api.product()
    error(api.call("POST", "/orders", order(product, 3, "NONE"), api.user), 422, "PROMO_CODE_INVALID")
    assert stock(product) == 10
    assert all(count(table) == 0 for table in ["orders", "order_items", "user_operations"])
    api.ok(201, "POST", "/orders", order(product), api.user)
    error(api.call("POST", "/orders", order(product), api.user), 429, "ORDER_LIMIT_EXCEEDED")
    age()
    error(api.call("POST", "/orders", order(product), api.user), 409, "ORDER_HAS_ACTIVE")


def test_all_shortages_and_catalog_errors(api):
    a, b = api.product(1), api.product(2)
    body = {"items": order(a, 3)["items"] + order(b, 4)["items"]}
    result = api.call("POST", "/orders", body, api.user)
    error(result, 409, "INSUFFICIENT_STOCK")
    assert len(result.json()["details"]["products"]) == 2
    assert stock(a) == 1 and stock(b) == 2
    api.ok(204, "DELETE", f"/products/{a}", token=api.seller)
    error(api.call("POST", "/orders", order(a), api.user), 409, "PRODUCT_INACTIVE")
    error(api.call("POST", "/orders", order(str(uuid4())), api.user), 404, "PRODUCT_NOT_FOUND")
    error(api.call("POST", "/orders", {"items": order(b)["items"] * 2}, api.user), 400, "VALIDATION_ERROR")


def test_update_rollback_price_and_rate(api):
    a, b = api.product(), api.product(1)
    created = api.ok(201, "POST", "/orders", order(a, 3), api.user)
    path = f"/orders/{created['id']}"
    error(api.call("PUT", path, order(b, 5), api.user), 409, "INSUFFICIENT_STOCK")
    assert stock(a) == 7 and stock(b) == 1 and count("user_operations") == 1
    sql("UPDATE products SET price=200 WHERE id=%s", (a,))
    changed = api.ok(200, "PUT", path, order(a, 5), api.user)
    assert stock(a) == 5 and changed["total_amount"] == 500
    error(api.call("PUT", path, order(a, 4), api.user), 429, "ORDER_LIMIT_EXCEEDED")


def test_promo_own_quota_drop_and_refund(api):
    a = api.product(20)
    promo(api, minimum=200)
    error(api.call("POST", "/orders", order(a, 1, "SAVE10"), api.user), 422, "PROMO_CODE_MIN_AMOUNT")
    created = api.ok(201, "POST", "/orders", order(a, 3, "SAVE10"), api.user)
    path = f"/orders/{created['id']}"
    assert api.ok(200, "PUT", path, order(a, 4), api.user)["total_amount"] == 360
    age()
    changed = api.ok(200, "PUT", path, order(a), api.user)
    assert changed["total_amount"] == 100 and changed["promo_code_id"] is None
    api.ok(200, "POST", path + "/cancel", token=api.user)
    assert sql("SELECT current_uses FROM promo_codes")[0][0] == 0


@pytest.mark.parametrize("change", ["active=false", "valid_until=clock_timestamp()-interval '1 hour'"])
def test_invalid_promo_update_rolls_back(api, change):
    a = api.product(20)
    promo(api)
    created = api.ok(201, "POST", "/orders", order(a, 3, "SAVE10"), api.user)
    sql("UPDATE promo_codes SET " + change)
    path = f"/orders/{created['id']}"
    error(api.call("PUT", path, order(a, 4), api.user), 422, "PROMO_CODE_INVALID")
    assert stock(a) == 17 and sql("SELECT current_uses FROM promo_codes")[0][0] == 1
    api.ok(200, "POST", path + "/cancel", token=api.user)
    assert stock(a) == 20 and sql("SELECT current_uses FROM promo_codes")[0][0] == 0


def test_roles_ownership_state_machine(api):
    a = api.product()
    created = api.ok(201, "POST", "/orders", order(a), api.user)
    path = f"/orders/{created['id']}"
    other = api.account("other")
    error(api.call("GET", path, token=other), 403, "ORDER_OWNERSHIP_VIOLATION")
    error(api.call("GET", path, token=api.seller), 403, "ACCESS_DENIED")
    error(api.call("POST", path + "/status", {"status": "PAID"}, api.user), 403, "ACCESS_DENIED")
    error(api.call("POST", path + "/status", {"status": "PAID"}, api.admin), 409, "INVALID_STATE_TRANSITION")
    for state in ["PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"]:
        api.ok(200, "POST", path + "/status", {"status": state}, api.admin)
        error(api.call("PUT", path, order(a, 2), api.user), 409, "INVALID_STATE_TRANSITION")
    error(api.call("POST", path + "/cancel", token=api.user), 409, "INVALID_STATE_TRANSITION")
    error(api.call("GET", f"/orders/{uuid4()}", token=api.user), 404, "ORDER_NOT_FOUND")


def test_access_refresh_expiry_escalation(api):
    from app.auth import now, token
    for raw in [None, "garbage"]:
        error(api.call("GET", "/products", token=raw), 401, "TOKEN_INVALID")
    pair = api.ok(200, "POST", "/auth/login", {"username": "buyer", "password": "Password-123"})
    error(api.call("GET", "/products", token=pair["refresh_token"]), 401, "TOKEN_INVALID")
    error(api.call("POST", "/auth/refresh", {"refresh_token": api.user}), 401, "REFRESH_TOKEN_INVALID")
    body = {"refresh_token": pair["refresh_token"]}
    api.ok(200, "POST", "/auth/refresh", body)
    error(api.call("POST", "/auth/refresh", body), 401, "REFRESH_TOKEN_INVALID")
    error(api.call("POST", "/auth/register", {"username": "hacker", "password": "Password-123", "role": "ADMIN"}), 400, "VALIDATION_ERROR")
    user = {"id": sql("SELECT id FROM users WHERE username='buyer'")[0][0], "role": "USER"}
    expired = token(user, "access", uuid4(), now() - timedelta(seconds=30))
    error(api.call("GET", "/products", token=expired), 401, "TOKEN_EXPIRED")


def test_concurrent_last_stock(api):
    a, other = api.product(1), api.account("racer")
    results = race(lambda: api.call("POST", "/orders", order(a), api.user), lambda: api.call("POST", "/orders", order(a), other))
    assert sorted(r.status_code for r in results) == [201, 409]
    assert next(r for r in results if r.status_code == 409).json()["error_code"] == "INSUFFICIENT_STOCK"
    assert stock(a) == 0 and count("orders") == 1


def test_concurrent_promo_last_use(api):
    a, b, other = api.product(), api.product(), api.account("racer")
    promo(api)
    results = race(lambda: api.call("POST", "/orders", order(a, 1, "SAVE10"), api.user),
                   lambda: api.call("POST", "/orders", order(b, 1, "SAVE10"), other))
    assert sorted(r.status_code for r in results) == [201, 422]
    assert stock(a) + stock(b) == 19 and count("orders") == 1
    assert sql("SELECT current_uses FROM promo_codes")[0][0] == 1


def test_concurrent_same_user(api):
    a = api.product()
    fn = lambda: api.call("POST", "/orders", order(a), api.user)
    assert sorted(r.status_code for r in race(fn, fn)) == [201, 429]
    assert count("orders") == 1 and stock(a) == 9


def test_concurrent_cancel_exactly_once(api):
    a = api.product()
    promo(api)
    created = api.ok(201, "POST", "/orders", order(a, 3, "SAVE10"), api.user)
    fn = lambda: api.call("POST", f"/orders/{created['id']}/cancel", token=api.user)
    assert sorted(r.status_code for r in race(fn, fn)) == [200, 409]
    assert stock(a) == 10 and sql("SELECT current_uses FROM promo_codes")[0][0] == 0


def test_nested_validation(api):
    a = api.product()
    for body in [order(a, 0), order(a, 1000), order(a, True), order("bad"), order(a, 1, "bad"),
                 {"items": []}, {"items": [None]}, {"items": order(a)["items"] * 51}]:
        error(api.call("POST", "/orders", body, api.user), 400, "VALIDATION_ERROR")
    assert count("orders") == 0 and stock(a) == 10


def test_money_rounding_and_overflow(api):
    from app.promos import discount
    assert discount(Decimal(100), "PERCENTAGE", Decimal(70)) == Decimal("70.00")
    assert discount(Decimal(100), "PERCENTAGE", Decimal("70.01")) == 0
    assert discount(Decimal(100), "FIXED_AMOUNT", Decimal(500)) == 100
    assert discount(Decimal("0.05"), "PERCENTAGE", Decimal(10)) == Decimal("0.01")
    a = api.product(2, 9999999999.99)
    error(api.call("POST", "/orders", order(a, 2), api.user), 400, "VALIDATION_ERROR")
    assert stock(a) == 2 and count("orders") == 0
    created = api.ok(201, "POST", "/orders", order(a), api.user)
    assert created["total_amount"] == 9999999999.99


def test_structured_logs_no_secrets(api, caplog):
    with caplog.at_level(logging.INFO, logger="marketplace.requests"):
        api.ok(200, "POST", "/auth/login", {"username": "buyer", "password": "Password-123"})
    entries = [json.loads(r.message) for r in caplog.records if r.name == "marketplace.requests"]
    assert entries[-1]["body"]["password"] == "***"
    assert "Password-123" not in caplog.text
    assert {"request_id", "method", "endpoint", "status_code", "duration_ms", "user_id", "timestamp"} <= entries[-1].keys()


def test_refresh_race_and_malformed_http(api):
    pair = api.ok(200, "POST", "/auth/login", {"username": "buyer", "password": "Password-123"})
    fn = lambda: api.call("POST", "/auth/refresh", {"refresh_token": pair["refresh_token"]})
    assert sorted(r.status_code for r in race(fn, fn)) == [200, 401]
    error(api.call("POST", "/products", token=api.seller, content=b'{"password":"do-not-log",'), 400, "VALIDATION_ERROR")
    error(api.call("GET", "/missing", token=api.user), 404, "NOT_FOUND")
    error(api.call("PATCH", "/products", token=api.user), 405, "METHOD_NOT_ALLOWED")


def test_flyway_index_and_timestamps(api):
    assert sql("SELECT success FROM flyway_schema_history WHERE version='1'") == [(True,)]
    assert sql("SELECT indexname FROM pg_indexes WHERE indexname='products_status_idx'")
    a = api.product()
    old = api.ok(200, "GET", f"/products/{a}", token=api.user)
    api.ok(204, "DELETE", f"/products/{a}", token=api.seller)
    new = api.ok(200, "GET", f"/products/{a}", token=api.user)
    assert new["created_at"] == old["created_at"] and new["updated_at"] > old["updated_at"]
