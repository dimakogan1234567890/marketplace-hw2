"""Common scalar validators; all DTO fields come from OpenAPI."""
from datetime import datetime
from decimal import Decimal


def number_only(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("expected a JSON number")
    return Decimal(str(value))


def datetime_only(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("expected an ISO 8601 datetime with timezone")
    return value
