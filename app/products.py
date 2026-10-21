from uuid import uuid4
from app.db import one, rows, transaction
from app.errors import require, role
from openapi_server.models.product_page import ProductPage
from openapi_server.models.product_response import ProductResponse


def find(db, product_id, lock=False):
    row = one(db, "SELECT * FROM products WHERE id=%s" + (" FOR UPDATE" if lock else ""), (product_id,))
    require(row is not None, 404, "PRODUCT_NOT_FOUND", "Товар не найден")
    return row


def get(actor, product_id):
    with transaction(read_only=True) as db:
        return ProductResponse.model_validate(find(db, product_id))


def list_products(actor, page, size, status, category):
    clauses, params = [], []
    if status is not None:
        clauses.append("status=%s")
        params.append(status.value)
    if category is not None:
        clauses.append("category=%s")
        params.append(category)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with transaction(read_only=True) as db:
        total = one(db, "SELECT count(*) AS n FROM products" + where, params)["n"]
        items = rows(db, "SELECT * FROM products" + where + " ORDER BY created_at,id LIMIT %s OFFSET %s",
                     [*params, size, page * size])
        return ProductPage(items=items, totalElements=total, page=page, size=size)


def values(body):
    return (body.name, body.description, body.price, body.stock, body.category, body.status.value)


def create(actor, body):
    role(actor, "SELLER", "ADMIN")
    with transaction() as db:
        row = one(db, "INSERT INTO products(id,seller_id,name,description,price,stock,category,status) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", (uuid4(), actor["id"], *values(body)))
        return ProductResponse.model_validate(row)


def own(actor, row):
    require(actor["role"] == "ADMIN" or actor["id"] == row["seller_id"], 403, "ACCESS_DENIED", "Чужой товар")


def update(actor, product_id, body):
    role(actor, "SELLER", "ADMIN")
    with transaction() as db:
        own(actor, find(db, product_id, True))
        row = one(db, "UPDATE products SET name=%s,description=%s,price=%s,stock=%s,category=%s,status=%s "
                      "WHERE id=%s RETURNING *", (*values(body), product_id))
        return ProductResponse.model_validate(row)


def delete(actor, product_id):
    role(actor, "SELLER", "ADMIN")
    with transaction() as db:
        own(actor, find(db, product_id, True))
        db.execute("UPDATE products SET status='ARCHIVED' WHERE id=%s AND status<>'ARCHIVED'", (product_id,))
