from __future__ import annotations

import asyncio
import dataclasses
import os
import sys

from skep.config import WorkerConfig, load_worker_config
from skep.db import Registry
from skep.discovery import resolve_queen_url
from skep.memory import MemoryPreflight
from skep.supervisor import Supervisor
from skep.transport import SwitchableEventSink, SwitchableMailboxClient
from skep.ws_transport import WorkerWsClient


def build_worker(
    wcfg: WorkerConfig,
) -> tuple[Supervisor, SwitchableEventSink, WorkerWsClient]:
    registry = Registry.open(wcfg.db_path)
    switch = SwitchableEventSink()
    mailbox_switch = SwitchableMailboxClient()
    supervisor = Supervisor(
        wcfg,
        registry,
        switch,
        mailbox_client=mailbox_switch,
        memory=MemoryPreflight(),
    )
    client = WorkerWsClient(
        wcfg, supervisor, switch, wcfg.shared_secret, mailbox_switch=mailbox_switch
    )
    return supervisor, switch, client


async def serve(wcfg: WorkerConfig) -> None:
    if not wcfg.shared_secret.strip():
        raise SystemExit(
            "SKEP_SHARED_SECRET is required (worker<->queen auth); "
            "refusing to start without it"
        )
    url = await resolve_queen_url(wcfg)
    if url is None:
        raise SystemExit(
            "no queen: set SKEP_QUEEN_URL or enable mDNS (SKEP_USE_MDNS=1)"
        )
    # freeze the resolved URL onto the config the client reads
    wcfg = dataclasses.replace(wcfg, queen_url=url)
    _sup, _switch, client = build_worker(wcfg)
    await client.run()


def run() -> None:
    try:
        asyncio.run(serve(load_worker_config(os.environ)))
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
