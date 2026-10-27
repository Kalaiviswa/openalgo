"""Transport helpers for Kite calls made over the shared httpx client.

On Windows the shared client multiplexes concurrent Kite calls over one HTTP/2
connection, and a read on that connection can fail with ``httpx.ReadError:
[WinError 10035]`` (issues #2067, #2293). The connection is dropped and the
next request opens a fresh one, so a read-only GET can simply be sent again.
Order placement can't: see ``order_api.place_order_api``.
"""

import httpx

from utils.logging import get_logger

logger = get_logger(__name__)

# The request may or may not have reached Kite, and the connection it used is
# gone. Timeouts are left out: retrying one would double an already long wait.
RETRYABLE_READ_ERRORS = (httpx.NetworkError, httpx.RemoteProtocolError)

# Raised before the request left OpenAlgo, so Kite never saw it.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def get_with_retry(client, url, headers):
    """GET once more if the connection breaks; safe because GET has no side effects."""
    try:
        return client.get(url, headers=headers)
    except RETRYABLE_READ_ERRORS as e:
        logger.warning(f"Kite GET failed ({e}), retrying once on a new connection")
        return client.get(url, headers=headers)
