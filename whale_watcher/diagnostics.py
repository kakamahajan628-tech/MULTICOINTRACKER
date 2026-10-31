"""Safe failure codes preserve operational evidence without serializing exceptions/URLs."""

import logging

import httpx


def failure_code(scope, error):
    expected = isinstance(error, (ValueError, httpx.HTTPError, OSError))
    kind = (
        "source_unavailable"
        if expected
        else "schema_error"
        if isinstance(error, KeyError)
        else "internal_error"
    )
    code = kind + ":" + type(error).__name__
    logging.getLogger(__name__).warning("%s failed (%s)", scope, code)
    return code
