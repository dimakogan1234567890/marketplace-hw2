"""Thin bridges called by generated HTTP handlers; there are no DTOs here."""
from app import auth
from app.orders import cancel as cancel_order, create as create_order, get as get_order
from app.orders import update as update_order, transition as transition_order
from app.products import create as create_product, delete as delete_product, get as get_product
from app.products import list_products, update as update_product
from app.promos import create as create_promo


def register(actor, body):
    return auth.register(body)


def login(actor, body):
    return auth.login(body)


def refresh(actor, body):
    return auth.refresh(body)
