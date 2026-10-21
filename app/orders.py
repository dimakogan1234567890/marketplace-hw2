import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from app import products, promos
from app.auth import now
from app.db import one, rows, transaction
from app.errors import Failure, require, role
from openapi_server.models.order_response import OrderResponse

INTERVAL_MINUTES = int(os.getenv("ORDER_INTERVAL_MINUTES", "1"))
require(INTERVAL_MINUTES >= 0, 500, "INTERNAL_ERROR", "Negative interval")
NEXT = {"CREATED": "PAYMENT_PENDING", "PAYMENT_PENDING": "PAID", "PAID": "SHIPPED", "SHIPPED": "COMPLETED"}


def rate(db, user_id, kind):
    last = one(db, "SELECT created_at FROM user_operations WHERE user_id=%s AND operation_type=%s "
                   "ORDER BY created_at DESC LIMIT 1", (user_id, kind))
    require(last is None or now() >= last["created_at"] + timedelta(minutes=INTERVAL_MINUTES),
            429, "ORDER_LIMIT_EXCEEDED", "Операция выполняется слишком часто")


def record(db, user_id, kind):
    db.execute("INSERT INTO user_operations(id,user_id,operation_type) VALUES (%s,%s,%s)", (uuid4(), user_id, kind))


def owned(db, actor, order_id, lock=False):
    role(actor, "USER", "ADMIN")
    row = one(db, "SELECT * FROM orders WHERE id=%s" + (" FOR UPDATE" if lock else ""), (order_id,))
    require(row is not None, 404, "ORDER_NOT_FOUND", "Заказ не найден")
    require(actor["role"] == "ADMIN" or row["user_id"] == actor["id"],
            403, "ORDER_OWNERSHIP_VIOLATION", "Чужой заказ")
    return row


def response(db, order):
    items = rows(db, "SELECT product_id,quantity,price_at_order FROM order_items WHERE order_id=%s ORDER BY product_id",
                 (order["id"],))
    return OrderResponse.model_validate({**order, "items": items})


def get(actor, order_id):
    with transaction(read_only=True) as db:
        return response(db, owned(db, actor, order_id))


def lock_products(db, items, old=()):
    ids = [item.product_id for item in items]
    require(len(ids) == len(set(ids)), 400, "VALIDATION_ERROR", "product_id не должен повторяться")
    all_ids = sorted(set(ids) | {item["product_id"] for item in old})
    return {product_id: products.find(db, product_id, True) for product_id in all_ids}


def reserve(db, items, locked):
    for item in items:
        require(locked[item.product_id]["status"] == "ACTIVE", 409, "PRODUCT_INACTIVE", "Товар неактивен")
    shortages = [{"product_id": item.product_id, "requested": item.quantity, "available": locked[item.product_id]["stock"]}
                 for item in items if locked[item.product_id]["stock"] < item.quantity]
    if shortages:
        raise Failure(409, "INSUFFICIENT_STOCK", "Недостаточно остатков", {"products": shortages})
    for item in items:
        db.execute("UPDATE products SET stock=stock-%s WHERE id=%s", (item.quantity, item.product_id))


def restore(db, old, locked):
    for item in old:
        db.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (item["quantity"], item["product_id"]))
        locked[item["product_id"]]["stock"] += item["quantity"]


def save_items(db, order_id, items, locked, old=()):
    prices = {item["product_id"]: item["price_at_order"] for item in old}
    db.execute("DELETE FROM order_items WHERE order_id=%s", (order_id,))
    gross = Decimal(0)
    for item in items:
        price = prices.get(item.product_id, locked[item.product_id]["price"])
        gross += price * item.quantity
        db.execute("INSERT INTO order_items(id,order_id,product_id,quantity,price_at_order) VALUES (%s,%s,%s,%s,%s)",
                   (uuid4(), order_id, item.product_id, item.quantity, price))
    require(gross <= Decimal("9999999999.99"), 400, "VALIDATION_ERROR", "Сумма превышает DECIMAL(12,2)")
    return gross


def totals(db, order_id, gross, promo):
    discount = promos.discount(gross, promo["discount_type"], promo["discount_value"]) if promo else Decimal(0)
    db.execute("UPDATE orders SET total_amount=%s,discount_amount=%s,promo_code_id=%s WHERE id=%s",
               (gross - discount, discount, promo["id"] if promo else None, order_id))


def create(actor, body):
    role(actor, "USER", "ADMIN")
    with transaction() as db:
        one(db, "SELECT id FROM users WHERE id=%s FOR UPDATE", (actor["id"],))
        rate(db, actor["id"], "CREATE_ORDER")
        require(one(db, "SELECT id FROM orders WHERE user_id=%s AND status IN ('CREATED','PAYMENT_PENDING')", (actor["id"],))
                is None, 409, "ORDER_HAS_ACTIVE", "Уже есть активный заказ")
        locked = lock_products(db, body.items)
        reserve(db, body.items, locked)
        order_id = uuid4()
        db.execute("INSERT INTO orders(id,user_id,status,total_amount) VALUES (%s,%s,'CREATED',0)", (order_id, actor["id"]))
        gross = save_items(db, order_id, body.items, locked)
        promo = None
        if body.promo_code is not None:
            promo = promos.find(db, body.promo_code, by_code=True)
            promos.valid(promo)
            require(gross >= promo["min_order_amount"], 422, "PROMO_CODE_MIN_AMOUNT", "Не достигнута минимальная сумма")
            promos.use(db, promo, 1)
        totals(db, order_id, gross, promo)
        record(db, actor["id"], "CREATE_ORDER")
        return response(db, owned(db, actor, order_id))


def update(actor, order_id, body):
    role(actor, "USER", "ADMIN")
    with transaction() as db:
        one(db, "SELECT id FROM users WHERE id=%s FOR UPDATE", (actor["id"],))
        order = owned(db, actor, order_id, True)
        require(order["status"] == "CREATED", 409, "INVALID_STATE_TRANSITION", "Обновление доступно только в CREATED")
        rate(db, actor["id"], "UPDATE_ORDER")
        old = rows(db, "SELECT * FROM order_items WHERE order_id=%s", (order_id,))
        locked = lock_products(db, body.items, old)
        restore(db, old, locked)
        reserve(db, body.items, locked)
        gross = save_items(db, order_id, body.items, locked, old)
        promo = None
        if order["promo_code_id"] is not None:
            promo = promos.find(db, order["promo_code_id"])
            if gross < promo["min_order_amount"]:
                promos.use(db, promo, -1)
                promo = None
            else:
                promos.valid(promo, own_use=True)
        totals(db, order_id, gross, promo)
        record(db, actor["id"], "UPDATE_ORDER")
        return response(db, owned(db, actor, order_id))


def cancel(actor, order_id):
    with transaction() as db:
        order = owned(db, actor, order_id, True)
        require(order["status"] in ("CREATED", "PAYMENT_PENDING"), 409, "INVALID_STATE_TRANSITION", "Отмена только до оплаты")
        old = rows(db, "SELECT * FROM order_items WHERE order_id=%s", (order_id,))
        locked = lock_products(db, [], old)
        restore(db, old, locked)
        if order["promo_code_id"] is not None:
            promos.use(db, promos.find(db, order["promo_code_id"]), -1)
        db.execute("UPDATE orders SET status='CANCELED' WHERE id=%s", (order_id,))
        return response(db, owned(db, actor, order_id))


def transition(actor, order_id, body):
    role(actor, "ADMIN")
    with transaction() as db:
        order = owned(db, actor, order_id, True)
        require(body.status.value == NEXT.get(order["status"]), 409, "INVALID_STATE_TRANSITION", "Недопустимый переход")
        db.execute("UPDATE orders SET status=%s WHERE id=%s", (body.status.value, order_id))
        return response(db, owned(db, actor, order_id))
