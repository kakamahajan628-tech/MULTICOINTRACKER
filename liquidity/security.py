"""Shared dashboard authentication and bounded, process-local failure throttling."""

import base64
import binascii
from collections import OrderedDict
import hmac
import ipaddress
import logging
import math
import threading
import time

MIN_TOKEN_LENGTH = 32


def validate_token(token, variable):
    if (
        len(token) < MIN_TOKEN_LENGTH
        or len(token) > 256
        or any(not 33 <= ord(char) <= 126 for char in token)
        or token.startswith("replace-with-")
    ):
        raise ValueError(
            f"{variable} must be 32–256 printable ASCII characters without spaces; generate a random token"
        )
    return token.encode("ascii")


def private_http_logging():
    # Telegram API paths contain the bot token. Never emit HTTP request INFO logs.
    for name in ("httpx", "httpcore", "aiohttp.client"):
        logging.getLogger(name).setLevel(logging.WARNING)


class FailureLimiter:
    def __init__(self, limit=5, window=60, capacity=10000, clock=time.monotonic):
        if limit < 1 or window < 1 or capacity < 1:
            raise ValueError("Invalid authentication limiter configuration")
        self.limit, self.window, self.capacity, self.clock = limit, window, capacity, clock
        self.clients = OrderedDict()
        self.lock = threading.Lock()

    def failure(self, peer):
        now = self.clock()
        with self.lock:
            # Fixed windows are ordered by creation time, so cleanup is bounded.
            while self.clients and next(iter(self.clients.values()))[0] + self.window <= now:
                self.clients.popitem(last=False)
            entry = self.clients.get(peer)
            if entry is None:
                if len(self.clients) >= self.capacity:
                    # Do not evict active blocked clients and let rotating IPs reset them.
                    retry = next(iter(self.clients.values()))[0] + self.window - now
                    return max(1, math.ceil(retry))
                entry = [now, 0]
                self.clients[peer] = entry
            entry[1] += 1
            return max(1, math.ceil(entry[0] + self.window - now)) if entry[1] > self.limit else 0


class DashboardAuth:
    def __init__(self, token, variable, scheme="basic", limiter=None):
        self.token = validate_token(token, variable)
        self.scheme = scheme
        self.limiter = limiter or FailureLimiter()

    def authenticate(self, header, peer):
        supplied = b""
        user_ok = True
        try:
            scheme, separator, value = header.partition(" ")
            if not separator or scheme.lower() != self.scheme:
                raise ValueError("Wrong authentication scheme")
            if self.scheme == "basic":
                raw = base64.b64decode(value, validate=True)
                username, separator, supplied = raw.partition(b":")
                user_ok = bool(separator) & hmac.compare_digest(username, b"whale")
            else:
                supplied = value.encode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            user_ok = False
        valid = hmac.compare_digest(supplied, self.token) & user_ok
        # Correct credentials remain usable when another client shares the same NAT.
        if valid:
            return 0
        retry = self.limiter.failure(peer or "unknown")
        return retry or -1  # positive retry seconds -> 429; -1 -> 401


def is_loopback(peer):
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return peer == "localhost"
