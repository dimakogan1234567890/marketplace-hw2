from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from app.auth import now
from app.db import one, transaction
from app.errors import require, role
from openapi_server.models.promo_response import PromoResponse


def create(actor, body):
    role(actor, "SELLER", "ADMIN")
    require(body.valid_until >= body.valid_from, 400, "VALIDATION_ERROR", "valid_until раньше valid_from")
    with transaction() as db:
        row = one(db, "INSERT INTO promo_codes(id,code,discount_type,discount_value,min_order_amount,max_uses,"
                      "valid_from,valid_until,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                      "ON CONFLICT(code) DO NOTHING RETURNING *",
                  (uuid4(), body.code, body.discount_type.value, body.discount_value, body.min_order_amount,
                   body.max_uses, body.valid_from, body.valid_until, body.active))
        require(row is not None, 409, "PROMO_CODE_EXISTS", "Код уже существует")
        return PromoResponse.model_validate(row)


def find(db, value, by_code=False):
    column = "code" if by_code else "id"
    row = one(db, f"SELECT * FROM promo_codes WHERE {column}=%s FOR UPDATE", (value,))
    require(row is not None, 422, "PROMO_CODE_INVALID", "Промокод не найден")
    return row


def valid(promo, own_use=False):
    used = promo["current_uses"] - int(own_use)
    require(promo["active"] and used < promo["max_uses"] and promo["valid_from"] <= now() <= promo["valid_until"],
            422, "PROMO_CODE_INVALID", "Промокод неактивен, истёк или исчерпан")


def use(db, promo, delta):
    db.execute("UPDATE promo_codes SET current_uses=current_uses+%s WHERE id=%s", (delta, promo["id"]))


def discount(gross, kind, value):
    if kind == "PERCENTAGE":
        amount = gross * value / 100 if value <= 70 else Decimal(0)
    else:
        amount = min(gross, value)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
