"""Linha de comando: `python -m zoio serve | go2rtc-config ARQUIVO | check | aparelhos`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta

from aiohttp import web

from . import config, devices, dvrip, go2rtc
from .app import create_app
from .store import Store

STATUS = {devices.PENDING: "aguardando", devices.APPROVED: "aprovado", devices.REVOKED: "revogado"}


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


def manage_devices(cfg: config.Config, action: str | None, ref: str | None, common: bool) -> int:
    """Lista, aprova ou revoga aparelhos direto no banco (o servidor vê na hora)."""
    store = Store(cfg.data_dir / "zoio.db")
    manager = devices.Devices(store, cfg.session_days)
    try:
        if action is None or action == "listar":
            items = store.devices()
            if not items:
                print("Nenhum aparelho cadastrado. Abra o Zóio no aparelho para ele aparecer aqui.")
            for d in items:
                seen = datetime.fromtimestamp(d["last_seen"]).strftime("%d/%m %H:%M") if d["last_seen"] else "nunca"
                admin = " (admin)" if d["admin"] else ""
                print(f"{d['code']}  {STATUS.get(d['status'], d['status']):<10} {d['name']}{admin}  · visto: {seen}  · id {d['id']}")
            if not cfg.device_approval:
                print("Atenção: a aprovação de aparelhos está desligada (ZOIO_DEVICE_APPROVAL=0).")
            return 0
        if not ref:
            print("Informe o código do aparelho (ex.: ABCD-EFGH) ou o id.", file=sys.stderr)
            return 2
        device = manager.find(ref)
        if action == "aprovar":
            device = manager.approve(device["id"], admin=not common, by="pi")
            kind = "comum" if common else "administrador (pode aprovar outros)"
            print(f"Aprovado: {device['name']} ({device['code']}), {kind}.")
        elif action == "revogar":
            manager.revoke(device["id"])
            print(f"Revogado: {device['name']} ({device['code']}). As sessões dele caíram.")
        elif action == "excluir":
            manager.delete(device["id"])
            print(f"Excluído: {device['name']} ({device['code']}). Ele pode pedir acesso de novo.")
        return 0
    except devices.DeviceError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="zoio")
    parser.add_argument("--env", help="arquivo de configuração (padrão: /etc/zoio/zoio.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="inicia o servidor")
    g = sub.add_parser("go2rtc-config", help="gera a configuração do go2rtc")
    g.add_argument("path")
    sub.add_parser("check", help="testa a conexão com o DVR")
    d = sub.add_parser("aparelhos", help="lista, aprova ou revoga aparelhos")
    d.add_argument("acao", nargs="?", choices=["listar", "aprovar", "revogar", "excluir"])
    d.add_argument("codigo", nargs="?", help="código mostrado no aparelho (ex.: ABCD-EFGH) ou id")
    d.add_argument("--comum", action="store_true", help="aprova sem permissão de aprovar outros aparelhos")
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
    elif args.cmd == "aparelhos":
        sys.exit(manage_devices(cfg, args.acao, args.codigo, args.comum))


if __name__ == "__main__":
    main()
