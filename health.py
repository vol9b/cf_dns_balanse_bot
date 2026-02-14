import asyncio
import logging
from typing import List


# Default ports to try for TCP health check
DEFAULT_CHECK_PORTS: List[int] = [443, 80]


async def check_tcp(
    address: str, port: int, timeout_seconds: float = 2
) -> bool:
    """Try to open a TCP connection to address:port."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port),
            timeout=timeout_seconds,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def ping_once(
    address: str, timeout_seconds: int = 2,
    ports: List[int] = None
) -> bool:
    """Check host availability via TCP connect.

    Tries each port in order; returns True on the first successful
    connection.  Falls back to ICMP only if no ports are specified
    (not recommended — many servers block ICMP).
    """
    check_ports = ports if ports is not None else DEFAULT_CHECK_PORTS

    for port in check_ports:
        if await check_tcp(address, port, timeout_seconds):
            return True

    # All TCP ports failed
    logging.debug(
        f"TCP check failed for {address} on ports {check_ports}"
    )
    return False
