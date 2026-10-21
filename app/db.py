import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://market:market@localhost:5432/marketplace")


@contextmanager
def transaction(read_only=False):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as db:
        if read_only:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield db
    # Successful exit commits; an exception rolls back; both paths close the connection.


def one(db, sql, params=()):
    return db.execute(sql, params).fetchone()


def rows(db, sql, params=()):
    return db.execute(sql, params).fetchall()
