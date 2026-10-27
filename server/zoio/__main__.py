"""Linha de comando: `python -m zoio serve | go2rtc-config ARQUIVO | check`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from aiohttp import web

from . import config, dvrip, go2rtc
from .app import create_app


async def check(cfg: config.Config) -> None:
    """Testa login, busca de gravações e alarmes, sem mexer em nada no DVR."""
    print(f"DVR {cfg.dvr_host}:{cfg.dvr_port}…")
    async with dvrip.Connection(cfg.dvr_host, cfg.dvr_port, cfg.dvr_user, cfg.dvr_pass) as conn:
        print(f"  login ok (sessão 0x{conn.session:08X}, keepalive {conn.alive_interval}s)")
        end = datetime.now()
        begin = end - timedelta(hours=24)
        fmt = "%Y-%m-%d %H:%M:%S"
        for cam in cfg.cameras:
            files = await conn.query_files(cam.number - 1, begin.strftime(fmt), end.strftime(fmt))
            print(f"  {cam.name}: {len(files)} gravações nas últimas 24h")
        events: list[dict] = []
        await conn.subscribe_alarms(events.append)
        print("  ouvindo alarmes por 15s…")
        await asyncio.sleep(15)
        print(f"  {len(events)} alarmes recebidos")
        for e in events[:5]:
            print(f"    {e}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="zoio")
    parser.add_argument("--env", help="arquivo de configuração (padrão: /etc/zoio/zoio.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="inicia o servidor")
    g = sub.add_parser("go2rtc-config", help="gera a configuração do go2rtc")
    g.add_argument("path")
    sub.add_parser("check", help="testa a conexão com o DVR")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    cfg = config.load(args.env)

    if args.cmd == "serve":
        web.run_app(create_app(cfg), host=cfg.listen_host, port=cfg.listen_port, print=None)
    elif args.cmd == "go2rtc-config":
        print(go2rtc.write(cfg, args.path))
    elif args.cmd == "check":
        asyncio.run(check(cfg))


if __name__ == "__main__":
    main()
