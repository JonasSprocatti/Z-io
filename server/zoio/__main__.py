"""Linha de comando: `python -m zoio serve | go2rtc-config ARQUIVO | check | aparelhos | portao`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

from . import clips, config, devices, dvrip, esphome, go2rtc
from .alarms import EventBus
from .app import create_app
from .esphome import EspHomeError
from .porteiro import GateError, Porteiro
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


def manage_devices(cfg: config.Config, action: str | None, ref: str | None, common: bool, deny: bool = False) -> int:
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
                extras = (" (admin)" if d["admin"] else "") + (" (portaria)" if d["gate"] else "")
                print(f"{d['code']}  {STATUS.get(d['status'], d['status']):<10} {d['name']}{extras}  · visto: {seen}  · id {d['id']}")
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
        elif action == "portao":
            device = manager.set_gate(device["id"], not deny)
            print(f"{device['name']} ({device['code']}) {'pode' if device['gate'] else 'não pode mais'} usar a portaria (portão, garagens e campainha).")
        return 0
    except devices.DeviceError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def manage_porteiro(cfg: config.Config, action: str, door: str | None, seconds: float = 60) -> int:
    """Testa a placa do porteiro, abre o portão, aciona uma garagem ou escuta o rádio pelo Pi."""
    if cfg.gate_error:
        print(f"Erro na configuração: {cfg.gate_error}", file=sys.stderr)
        return 1
    if not cfg.gate_enabled:
        print("Portaria não configurada: defina ZOIO_GATE_HOST e ZOIO_GATE_KEY no /etc/zoio/zoio.env.", file=sys.stderr)
        return 1

    async def radio(porteiro: Porteiro) -> int:
        print(f"Escutando o rádio da placa por {seconds:.0f} s. Aperte cada botão do controle perto dela,")
        print("um de cada vez, e abra e feche cada garagem que tiver sensor sem fio.", flush=True)
        codes: list[tuple[int, str]] = []

        def on_code(protocol: int, code: str) -> None:
            codes.append((protocol, code))
            print(f"  {len(codes)}. protocolo {protocol}, código {code} ({len(code)} bits)", flush=True)

        await porteiro.radio(seconds, on_code)
        if not codes:
            print("Nenhum código chegou. Confira o receptor (DATA no GPIO8, VCC no 3V3) e a antena.", file=sys.stderr)
            return 1
        print("Anote qual botão ou sensor gerou cada código e ponha no secrets.yaml da placa")
        print("(radio_protocolo, radio_garagem_N, sensor_protocolo, sensor_garagem_N_abriu/_fechou).")
        return 0

    async def run() -> int:
        store = Store(cfg.data_dir / "zoio.db")
        porteiro = Porteiro(cfg, store, EventBus())
        try:
            if action == "radio":
                return await radio(porteiro)
            async with porteiro.session():
                info = porteiro.info
                if action == "testar":
                    print(f"Placa {info['name']} ({info['mac'] or 'MAC ?'}), {info['version']}")
                    print(f"  botões: {', '.join(info['buttons']) or 'nenhum'}")
                    print(f"  sensores: {', '.join(info['sensors']) or 'nenhum'}")
                    for d in porteiro.doors.values():
                        extra = "" if d.kind == "portao" else (" (com sensor)" if d.sensor else " (sem sensor: só acionar)")
                        print(f"  porta {d.id}: {d.name}{extra}" + (f", câmera {d.camera}" if d.camera else ""))
                    print("  campainha: " + ("ok" if info["doorbell"] else "não encontrada"))
                    if "portao" not in porteiro.doors:
                        print("  atenção: não achei o botão abrir_portao.")
                        return 1
                    return 0
                target = "portao" if action == "abrir" else (door or "")
                if target not in porteiro.doors:
                    print(f"Porta desconhecida: {target or '(nenhuma)'}. Portas: {', '.join(porteiro.doors) or 'nenhuma'}", file=sys.stderr)
                    return 2
                async with aiohttp.ClientSession() as http:
                    porteiro.http = http
                    event = await porteiro.act(target, "abrir" if target == "portao" else "acionar", "Pi", None)
            if event["detail"]["error"]:
                print(f"Falhou: {event['detail']['error']}", file=sys.stderr)
                return 1
            print(f"{event['detail']['name']}: {event['status'].lower()}." + (" Foto salva." if event["detail"]["photo"] else ""))
            return 0
        except (EspHomeError, GateError) as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1
        finally:
            store.close()

    return asyncio.run(run())


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
    d.add_argument("acao", nargs="?", choices=["listar", "aprovar", "revogar", "excluir", "portao"])
    d.add_argument("codigo", nargs="?", help="código mostrado no aparelho (ex.: ABCD-EFGH) ou id")
    d.add_argument("--comum", action="store_true", help="aprova sem permissão de aprovar outros aparelhos nem de usar a portaria")
    d.add_argument("--nao", action="store_true", help="com 'portao': tira a permissão de portaria")
    pt = sub.add_parser("portao", help="placa do porteiro: gerar chave, testar, abrir o portão, acionar uma garagem ou escutar o rádio")
    pt.add_argument("acao", choices=["chave", "testar", "abrir", "acionar", "radio"])
    pt.add_argument("porta", nargs="?", help="com 'acionar': garagem_1, garagem_2...")
    pt.add_argument("--segundos", type=float, default=60, help="com 'radio': quanto tempo escutar (padrão 60)")
    args = parser.parse_args()

    if args.cmd == "portao" and args.acao == "chave":  # não precisa da configuração
        print(esphome.new_key())
        print("Use a mesma chave em ZOIO_GATE_KEY (/etc/zoio/zoio.env) e em api_key (secrets da placa).", file=sys.stderr)
        return

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
        sys.exit(manage_devices(cfg, args.acao, args.codigo, args.comum, args.nao))
    elif args.cmd == "portao":
        sys.exit(manage_porteiro(cfg, args.acao, args.porta, args.segundos))


if __name__ == "__main__":
    main()
