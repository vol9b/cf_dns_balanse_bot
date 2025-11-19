import asyncio

async def ping_once(address: str, timeout_seconds: int = 2) -> bool:
    """Асинхронный ICMP пинг"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout_seconds), address,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds + 1)
            return proc.returncode == 0
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False
    except Exception:
        return False
