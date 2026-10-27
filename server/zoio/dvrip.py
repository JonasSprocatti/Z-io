"""Cliente assíncrono mínimo do protocolo DVRIP/Sofia (Xiongmai), porta 34567.

Cada pacote tem um cabeçalho de 20 bytes seguido do corpo:

    0xFF, versão, 2 reservados, sessão (u32), sequência (u32),
    total/atual (2 x u8), código da mensagem (u16), tamanho do corpo (u32)

Os comandos levam JSON terminado em "\\n\\0". As respostas usam, em geral,
o código do pedido + 1. Alarmes chegam sem pedido, com o código 1504.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
from typing import AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

HEADER = struct.Struct("<BB2xIIBBHI")

LOGIN = 1000
KEEPALIVE = 1006
PLAYBACK = 1420
PLAYBACK_CLAIM = 1424
PLAYBACK_DATA = 1426
FILE_QUERY = 1440
TIME_QUERY = 1452
ALARM_SET = 1500
ALARM_INFO = 1504

OK = (100, 515)  # 515 = "atualização bem-sucedida", também aceito como sucesso

ERRORS = {
    101: "erro desconhecido",
    103: "comando não suportado",
    106: "usuário sem permissão",
    203: "senha incorreta",
    205: "usuário bloqueado",
    206: "usuário bloqueado temporariamente",
}


class DVRIPError(Exception):
    pass


def sofia_hash(password: str) -> str:
    """Hash de senha usado no login (MD5 dobrado em 8 caracteres base62)."""
    digest = hashlib.md5(password.encode()).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(a + b) % 62] for a, b in zip(digest[::2], digest[1::2]))


def _decode_json(body: bytes) -> dict:
    return json.loads(body.rstrip(b"\x00\n").decode("utf-8", "replace"))


class Connection:
    """Uma sessão TCP autenticada no DVR.

    Um único leitor despacha os pacotes: respostas vão para o pedido em
    espera, alarmes para `on_alarm` e dados de reprodução para a fila de
    download ativa. Os pedidos são serializados por um lock.
    """

    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 10.0):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.timeout = timeout
        self.session = 0
        self.alive_interval = 20
        self.on_alarm: Callable[[dict], Awaitable[None] | None] | None = None
        self._seq = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._waiting: tuple[int, asyncio.Future] | None = None
        self._stream: asyncio.Queue[bytes | None] | None = None
        self.closed = asyncio.Event()

    async def __aenter__(self) -> "Connection":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        self._read_task = asyncio.create_task(self._read_loop())
        reply = await self.request(
            LOGIN,
            {
                "EncryptType": "MD5",
                "LoginType": "DVRIP-Web",
                "PassWord": sofia_hash(self.password),
                "UserName": self.user,
            },
        )
        ret = reply.get("Ret")
        if ret not in OK:
            await self.close()
            raise DVRIPError(f"login recusado ({ret}: {ERRORS.get(ret, 'código desconhecido')})")
        self.session = int(reply["SessionID"], 16)
        self.alive_interval = int(reply.get("AliveInterval") or 20)
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def close(self) -> None:
        for task in (self._keepalive_task, self._read_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            self._writer = None
        self._fail_pending(DVRIPError("conexão encerrada"))
        self.closed.set()

    def _fail_pending(self, exc: Exception) -> None:
        if self._waiting and not self._waiting[1].done():
            self._waiting[1].set_exception(exc)
        if self._stream:
            self._stream.put_nowait(None)

    def _send(self, msgid: int, payload: dict | bytes) -> None:
        if self._writer is None:
            raise DVRIPError("não conectado")
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode() + b"\n\x00"
        self._writer.write(HEADER.pack(0xFF, 0, self.session, self._seq, 0, 0, msgid, len(body)) + body)
        self._seq += 1

    async def request(self, msgid: int, payload: dict, reply_id: int | None = None) -> dict:
        async with self._lock:
            fut = asyncio.get_running_loop().create_future()
            self._waiting = (reply_id or msgid + 1, fut)
            self._send(msgid, payload)
            await self._writer.drain()
            try:
                return await asyncio.wait_for(fut, self.timeout)
            finally:
                self._waiting = None

    async def _read_loop(self) -> None:
        try:
            while True:
                header = await self._reader.readexactly(HEADER.size)
                _, _, session, _, _, _, msgid, length = HEADER.unpack(header)
                body = await self._reader.readexactly(length) if length else b""
                await self._dispatch(msgid, body)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            log.debug("leitura DVRIP encerrada: %s", exc)
        except asyncio.CancelledError:
            return
        finally:
            self._fail_pending(DVRIPError("conexão com o DVR caiu"))
            self.closed.set()

    async def _dispatch(self, msgid: int, body: bytes) -> None:
        if msgid == ALARM_INFO:
            if self.on_alarm:
                try:
                    data = _decode_json(body)
                    result = self.on_alarm(data.get(data.get("Name", "AlarmInfo"), data))
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("falha ao tratar alarme")
            return
        if self._stream is not None and msgid in (PLAYBACK_DATA, PLAYBACK + 1):
            if msgid == PLAYBACK + 1 and body[:1] == b"{":
                # Resposta JSON ao DownloadStart; se for erro, encerra o fluxo.
                if _decode_json(body).get("Ret") not in OK:
                    self._stream.put_nowait(None)
                return
            self._stream.put_nowait(body if body else None)
            return
        if self._waiting and msgid == self._waiting[0] and not self._waiting[1].done():
            try:
                self._waiting[1].set_result(_decode_json(body) if body else {})
            except ValueError as exc:
                self._waiting[1].set_exception(DVRIPError(f"resposta inválida: {exc}"))
            return
        log.debug("pacote DVRIP ignorado: %d (%d bytes)", msgid, len(body))

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.alive_interval)
                if self._stream is not None:
                    continue  # durante o download o DVR já sabe que estamos vivos
                await self.request(KEEPALIVE, {"Name": "KeepAlive", "SessionID": f"0x{self.session:08X}"})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("keepalive falhou: %s", exc)
            await self.close()

    # ---- comandos ----

    async def subscribe_alarms(self, callback: Callable[[dict], Awaitable[None] | None]) -> None:
        self.on_alarm = callback
        reply = await self.request(ALARM_SET, {"Name": "", "SessionID": f"0x{self.session:08X}"})
        if reply.get("Ret") not in OK:
            raise DVRIPError(f"DVR recusou a inscrição de alarmes ({reply.get('Ret')})")

    async def clock(self) -> str:
        """Hora atual do DVR, como ele mostra ('AAAA-MM-DD HH:MM:SS')."""
        reply = await self.request(TIME_QUERY, {"Name": "OPTimeQuery", "SessionID": f"0x{self.session:08X}"})
        if reply.get("Ret") not in OK:
            raise DVRIPError(f"DVR recusou a consulta da hora ({reply.get('Ret')})")
        return str(reply.get("OPTimeQuery", ""))

    async def query_files(self, channel: int, begin: str, end: str, kind: str = "h264") -> list[dict]:
        """Lista arquivos gravados. `channel` começa em 0; horários 'AAAA-MM-DD HH:MM:SS'."""
        results: list[dict] = []
        seen: set[str] = set()
        start = begin
        for _ in range(32):  # o DVR devolve no máximo 64 por vez
            reply = await self.request(
                FILE_QUERY,
                {
                    "Name": "OPFileQuery",
                    "OPFileQuery": {
                        "BeginTime": start,
                        "Channel": channel,
                        "DriverTypeMask": "0x0000FFFF",
                        "EndTime": end,
                        "Event": "*",
                        "StreamType": "0x00000000",
                        "Type": kind,
                    },
                },
            )
            if reply.get("Ret") not in OK:
                break
            page = reply.get("OPFileQuery") or []
            new = [f for f in page if f.get("FileName") not in seen]
            for f in new:
                seen.add(f["FileName"])
            results.extend(new)
            if len(page) < 64 or not new:
                break
            start = page[-1]["BeginTime"]
        return results

    async def download(self, filename: str, begin: str, end: str) -> AsyncIterator[bytes]:
        """Baixa um arquivo gravado e devolve os pedaços no formato privado do DVR."""
        params = {
            "PlayMode": "ByName",
            "FileName": filename,
            "StreamType": 0,
            "Value": 0,
            "TransMode": "TCP",
        }
        claim = await self.request(
            PLAYBACK_CLAIM,
            {"Name": "OPPlayBack", "OPPlayBack": {"Action": "Claim", "Parameter": params, "StartTime": begin, "EndTime": end}},
        )
        if claim.get("Ret") not in OK:
            raise DVRIPError(f"DVR recusou a reprodução ({claim.get('Ret')})")
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        async with self._lock:
            self._stream = queue
            self._send(
                PLAYBACK,
                {"Name": "OPPlayBack", "OPPlayBack": {"Action": "DownloadStart", "Parameter": params, "StartTime": begin, "EndTime": end}},
            )
            await self._writer.drain()
            try:
                while True:
                    chunk = await asyncio.wait_for(queue.get(), self.timeout * 3)
                    if chunk is None:
                        break
                    yield chunk
            finally:
                self._stream = None
        if self._writer and not self.closed.is_set():
            try:
                await self.request(
                    PLAYBACK,
                    {"Name": "OPPlayBack", "OPPlayBack": {"Action": "DownloadStop", "Parameter": params, "StartTime": begin, "EndTime": end}},
                )
            except Exception as exc:  # o arquivo já veio inteiro; o stop é só cortesia
                log.debug("DownloadStop falhou: %s", exc)
