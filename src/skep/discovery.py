from __future__ import annotations

import asyncio
import socket
from typing import override

from zeroconf import ServiceInfo, ServiceListener, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from skep.config import WorkerConfig

SERVICE_TYPE = "_skep-queen._tcp.local."


class AsyncServiceHandle:
    def __init__(self, azc: AsyncZeroconf, info: ServiceInfo):
        self._azc = azc
        self._info = info

    async def close(self) -> None:
        await self._azc.async_unregister_service(self._info)
        await self._azc.async_close()


async def advertise(host: str, port: int, *, instance: str = "skep-queen",
                    public_url: str | None = None) -> AsyncServiceHandle:
    azc = AsyncZeroconf()
    properties: dict[bytes, bytes | None] = {b"host": instance.encode()}
    if public_url:
        properties[b"public_url"] = public_url.encode()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{instance}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(host)],
        port=port,
        properties=properties,
    )
    await azc.async_register_service(info)
    return AsyncServiceHandle(azc, info)


async def browse(timeout: float = 3.0) -> str | None:
    azc = AsyncZeroconf()
    found: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    async def _resolve(type_: str, name: str) -> None:
        info = await azc.async_get_service_info(type_, name, timeout=int(timeout * 1000))
        if info is None:
            return
        addrs = info.parsed_addresses()
        if addrs and info.port:
            found.put_nowait(f"ws://{addrs[0]}:{info.port}/ws")

    class _Listener(ServiceListener):
        @override
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            # Called synchronously from zeroconf's event loop dispatch;
            # schedule the async resolve rather than blocking it inline.
            task = loop.create_task(_resolve(type_, name))
            pending.add(task)
            task.add_done_callback(pending.discard)

        @override
        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        @override
        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    browser = AsyncServiceBrowser(azc.zeroconf, SERVICE_TYPE, _Listener())
    try:
        return await asyncio.wait_for(found.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await browser.async_cancel()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await azc.async_close()


async def resolve_queen_url(config: WorkerConfig, *,
                            browse_timeout: float = 3.0) -> str | None:
    if config.queen_url:
        return config.queen_url
    if config.use_mdns:
        return await browse(timeout=browse_timeout)
    return None
