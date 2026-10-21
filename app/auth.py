import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import bcrypt
import jwt
from app.db import one, transaction
from app.errors import Failure, require
from openapi_server.models.token_pair import TokenPair
from openapi_server.models.user_response import UserResponse

SECRET = os.getenv("JWT_SECRET", "local-demo-secret-change-me-at-least-32-bytes")
ACCESS_SECONDS = 20 * 60
REFRESH_SECONDS = 14 * 24 * 60 * 60
require(len(SECRET.encode()) >= 32, 500, "INTERNAL_ERROR", "JWT_SECRET too short")


def now():
    return datetime.now(timezone.utc)


def password_bytes(password):
    value = password.encode("utf-8")
    require(len(value) <= 72, 400, "VALIDATION_ERROR", "Пароль длиннее 72 байт UTF-8")
    return value


def bootstrap_admin():
    password = os.getenv("ADMIN_PASSWORD", "Admin-demo-password-123")
    hashed = bcrypt.hashpw(password_bytes(password), bcrypt.gensalt()).decode()
    with transaction() as db:
        db.execute("INSERT INTO users(id,username,password_hash,role) VALUES (%s,%s,%s,'ADMIN') "
                   "ON CONFLICT(username) DO NOTHING", (uuid4(), "admin", hashed))


def register(body):
    hashed = bcrypt.hashpw(password_bytes(body.password), bcrypt.gensalt()).decode()
    with transaction() as db:
        row = one(db, "INSERT INTO users(id,username,password_hash,role) VALUES (%s,%s,%s,%s) "
                      "ON CONFLICT(username) DO NOTHING RETURNING id,username,role",
                  (uuid4(), body.username, hashed, body.role))
        require(row is not None, 409, "USERNAME_TAKEN", "Имя уже занято")
        return UserResponse.model_validate(row)


def token(user, kind, jti, expires):
    return jwt.encode({"iss": "marketplace", "sub": str(user["id"]), "role": user["role"],
                       "type": kind, "jti": str(jti), "iat": now(), "exp": expires}, SECRET, algorithm="HS256")


def issue(db, user):
    jti = uuid4()
    expires = now() + timedelta(seconds=REFRESH_SECONDS)
    db.execute("INSERT INTO refresh_tokens(jti,user_id,expires_at) VALUES (%s,%s,%s)",
               (jti, user["id"], expires))
    return TokenPair(access_token=token(user, "access", uuid4(), now() + timedelta(seconds=ACCESS_SECONDS)),
                     refresh_token=token(user, "refresh", jti, expires), token_type="Bearer", expires_in=ACCESS_SECONDS)


def login(body):
    value = password_bytes(body.password)
    with transaction() as db:
        user = one(db, "SELECT * FROM users WHERE username=%s", (body.username,))
        require(user is not None and bcrypt.checkpw(value, user["password_hash"].encode()),
                401, "INVALID_CREDENTIALS", "Неверный логин или пароль")
        return issue(db, user)


def decode(raw, kind):
    code = "REFRESH_TOKEN_INVALID" if kind == "refresh" else "TOKEN_INVALID"
    try:
        claims = jwt.decode(raw, SECRET, algorithms=["HS256"], issuer="marketplace",
                            options={"require": ["iss", "sub", "role", "type", "jti", "iat", "exp"]})
        require(claims["type"] == kind, 401, code, "Неверный тип токена")
        claims["sub"], claims["jti"] = UUID(claims["sub"]), UUID(claims["jti"])
        return claims
    except jwt.ExpiredSignatureError:
        raise Failure(401, "TOKEN_EXPIRED" if kind == "access" else code, "Токен истёк") from None
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise Failure(401, code, "Некорректный токен") from None


def authenticate(header):
    require(header and header.startswith("Bearer "), 401, "TOKEN_INVALID", "Нужен Bearer access token")
    claims = decode(header[7:], "access")
    with transaction(read_only=True) as db:
        user = one(db, "SELECT id,role FROM users WHERE id=%s", (claims["sub"],))
        require(user is not None and user["role"] == claims["role"], 401, "TOKEN_INVALID", "Пользователь или роль изменились")
        return user


def refresh(body):
    claims = decode(body.refresh_token, "refresh")
    with transaction() as db:
        saved = one(db, "SELECT * FROM refresh_tokens WHERE jti=%s FOR UPDATE", (claims["jti"],))
        require(saved is not None and saved["user_id"] == claims["sub"] and not saved["revoked"]
                and saved["expires_at"] > now(), 401, "REFRESH_TOKEN_INVALID", "Токен отозван или истёк")
        user = one(db, "SELECT id,role FROM users WHERE id=%s", (claims["sub"],))
        require(user is not None and user["role"] == claims["role"], 401, "REFRESH_TOKEN_INVALID", "Пользователь или роль изменились")
        db.execute("UPDATE refresh_tokens SET revoked=true WHERE jti=%s", (claims["jti"],))
        return issue(db, user)
