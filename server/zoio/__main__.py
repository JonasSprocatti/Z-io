"""Linha de comando: `python -m zoio serve | go2rtc-config ARQUIVO | check | aparelhos`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta

from aiohttp import web

from . import clips, config, devices, dvrip, go2rtc
from .app import create_app
from .store import Store
from .xmstream import Demuxer

STATUS = {devices.PENDING: "aguardando", devices.APPROVED: "aprovado", devices.REVOKED: "revogado"}


FMT = "%Y-%m-%d %H:%M:%S"


async def check(cfg: config.Config, download: bool = False, alarm_seconds: float = 15) -> int:
    """Testa login, relógio, busca de gravações, alarmes e, se pedido, um
    download curto. Só lê do DVR, sem mudar nada nele. Devolve quantos testes falharam."""
    failures = 0
    newest: tuple[config.Camera, dict] | None = None
    print(f"DVR {cfg.dvr_host}:{cfg.dvr_port}…")
    async with dvrip.Connection(cfg.dvr_host, cfg.dvr_port, cfg.dvr_user, cfg.dvr_pass) as conn:
        print(f"  login ok (sessão 0x{conn.session:08X}, keepalive {conn.alive_interval}s)")

        try:
            shown = await conn.clock()
            local = datetime.now().astimezone()
            diff = (datetime.strptime(shown, FMT) - local.replace(tzinfo=None)).total_seconds()
            print(f"  relógio do DVR: {shown}; Pi: {local.strftime(FMT)} {local.tzname()} (diferença {diff:+.0f}s)")
            if abs(diff) > 120:
                print("  atenção: o relógio do DVR difere do Pi. Confira o fuso e o horário de verão nos dois.")
        except (dvrip.DVRIPError, ValueError, asyncio.TimeoutError) as exc:
            print(f"  relógio do DVR: não consegui ler ({exc or exc.__class__.__name__})")

        end = datetime.now()
        begin = end - timedelta(hours=24)
        for cam in cfg.cameras:
            files = await conn.query_files(cam.number - 1, begin.strftime(FMT), end.strftime(FMT))
            print(f"  {cam.name}: {len(files)} gravações nas últimas 24h")
            for f in files:
                if newest is None or f["BeginTime"] > newest[1]["BeginTime"]:
                    newest = (cam, f)
        if newest:
            print(f"  mais recente: {json.dumps(newest[1], ensure_ascii=False)}")

        events: list[dict] = []
        try:
            await conn.subscribe_alarms(events.append)
            print(f"  ouvindo alarmes por {alarm_seconds:g}s…")
            await asyncio.sleep(alarm_seconds)
            print(f"  {len(events)} alarmes recebidos")
            for e in events[:5]:
                print(f"    {e}")
        except (dvrip.DVRIPError, asyncio.TimeoutError) as exc:
            failures += 1
            print(f"  alarmes: FALHOU ({exc or 'sem resposta do DVR'}). Rode com -v para ver os pacotes.")

    if download:
        if newest is None:
            print("  download: nenhuma gravação nas últimas 24h para testar")
        else:
            try:
                await check_download(cfg, *newest)
            except Exception as exc:
                failures += 1
                print(f"  download: FALHOU ({exc or exc.__class__.__name__}). Rode com -v para ver os pacotes.")
    return failures


async def check_download(cfg: config.Config, cam: config.Camera, entry: dict, limit: int = 3 * 1024 * 1024) -> None:
    """Baixa só o começo (até `limit` bytes) de uma gravação e converte para MP4."""
    print(f"  baixando até {limit // 1024 // 1024} MB de {entry['FileName']} ({cam.name})…")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    raw, out = cfg.data_dir / "check.es", cfg.data_dir / "check.mp4"
    demux = Demuxer()
    received = 0
    started = time.monotonic()
    try:
        with raw.open("wb") as f:
            async with dvrip.Connection(cfg.dvr_host, cfg.dvr_port, cfg.dvr_user, cfg.dvr_pass) as conn:
                async with contextlib.aclosing(conn.download(entry["FileName"], entry["BeginTime"], entry["EndTime"])) as chunks:
                    async for chunk in chunks:
                        received += len(chunk)
                        f.write(demux.feed(chunk))
                        if received >= limit:
                            break
        elapsed = max(time.monotonic() - started, 0.001)
        info = demux.info
        print(
            f"  recebidos {received / 1e6:.1f} MB em {elapsed:.1f}s ({received / 1e6 / elapsed:.2f} MB/s): "
            f"{info.codec} {info.width}x{info.height} {info.fps} fps, {info.video_frames} quadros de vídeo, "
            f"{info.audio_frames} de áudio, {info.skipped_bytes} bytes ignorados"
        )
        if info.video_frames == 0:
            raise RuntimeError("o DVR não mandou vídeo")
        await clips.remux(raw, out, info.codec or "hevc", info.fps or 15)
    finally:
        raw.unlink(missing_ok=True)
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
        "-of", "compact=p=0:nk=1", str(out), stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    print(f"  MP4 ok: {out} ({' | '.join(stdout.decode().split())})")


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
    c = sub.add_parser("check", help="testa a conexão com o DVR")
    c.add_argument("--baixar", action="store_true", help="também baixa o começo da gravação mais recente e converte para MP4")
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
        sys.exit(1 if asyncio.run(check(cfg, download=args.baixar)) else 0)
    elif args.cmd == "aparelhos":
        sys.exit(manage_devices(cfg, args.acao, args.codigo, args.comum))


if __name__ == "__main__":
    main()
