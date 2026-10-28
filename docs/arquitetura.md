# Arquitetura do Zóio

## Restrições que guiaram as escolhas

| Restrição | Consequência |
|---|---|
| Raspberry Pi 3B (4 núcleos ARM fracos, 1 GB de RAM) | Nada de recodificar vídeo nem de análise de imagem no Pi. Só repasse e remux. |
| DVR Xiongmai AHB8004R-MH-NVT, firmware V4.03 | RTSP na porta 554 para o ao vivo. DVRIP na porta 34567 para gravações e alarmes (testado em 26/10/2026). |
| Tudo em H.265 hoje | O ao vivo depende de o navegador tocar H.265. A recomendação é passar o fluxo secundário para H.264. |
| Acesso fora de casa só para aparelhos autorizados | Tailscale. Nenhuma porta aberta no roteador, e a nuvem XMEye fica de fora do app. |
| Android, iOS, desktop e web | Um único app web instalável (PWA), sem lojas nem builds por plataforma. |

## Componentes

### go2rtc (`zoio-go2rtc.service`)
Binário único em Go, leve, que converte RTSP em MSE/HLS para o navegador sem recodificar. A configuração é gerada pelo Zóio a cada início (`python -m zoio go2rtc-config`) a partir do `/etc/zoio/zoio.env`. Assim a senha do DVR não fica em nenhum arquivo do repositório. Ele escuta só em `127.0.0.1:1984`, com RTSP, WebRTC e RTMP desligados.

Fluxos: `camN` (principal, para a tela cheia) e `camN_sub` (secundário, para a grade).

### zoio (`zoio.service`)
Servidor Python (aiohttp) com quatro papéis:

1. **App e API.** Serve o PWA (`server/zoio/web/`) e a API em `/api/*`.
2. **Proxy do ao vivo.** `/live/*` repassa ao go2rtc só o player (`video-stream.js`), o WebSocket e o HLS, e só para os fluxos conhecidos (`camN` e `camN_sub`). A API de administração do go2rtc, que permite criar fontes novas e até rodar comandos, nunca fica acessível.
3. **Gravações** (`clips.py`, `dvrip.py`, `xmstream.py`). Busca com `OPFileQuery` (paginada, 64 por vez) e download com `OPPlayBack`/`DownloadStart`. O fluxo privado da Xiongmai (quadros `00 00 01 FC/FD/FA/F9`) é desmontado em Annex B, e o ffmpeg faz o remux para MP4 (`-c copy`, com `hvc1` para o Safari tocar H.265). Um download por vez, com progresso, e cache em disco com limite (`ZOIO_CACHE_MAX_MB`).
4. **Alertas** (`alarms.py`, `push.py`). Uma conexão DVRIP fica permanentemente inscrita nos alarmes (`AlarmSet` 1500 → `AlarmInfo` 1504), com keepalive e reconexão automática. O DVR repete "Start" enquanto há movimento, e só as transições são gravadas no SQLite (retenção de 30 dias). As telas abertas recebem os eventos por SSE (`/api/events/stream`). As notificações usam Web Push (RFC 8291/8292) implementado direto com `cryptography`: o conteúdo vai cifrado, o serviço de push da Apple ou do Google só entrega, e o Pi só faz conexões de saída. Há um intervalo mínimo por câmera (`ZOIO_NOTIFY_COOLDOWN`).

### Tailscale serve
Publica `https://<pi>.<tailnet>.ts.net` → `http://127.0.0.1:8080`, com certificado válido (necessário para PWA, service worker e Web Push) e o cabeçalho `Tailscale-User-Login`, que o Zóio confere quando `ZOIO_ALLOWED_USERS` está definido.

## Aprovação de aparelhos (`devices.py`)

Mesmo dentro da VPN, cada aparelho precisa ser aprovado individualmente:

1. **Chave do aparelho.** O app gera um par ECDSA P-256 pela WebCrypto com `extractable: false` e guarda no IndexedDB. Nem o JavaScript do app consegue ler a chave privada, só pedir assinaturas.
2. **Cadastro.** `POST /api/devices/register` envia a chave pública (JWK). O servidor valida o ponto na curva e calcula o **código** (8 caracteres base32 do SHA-256 da chave, 40 bits), que aparece na tela do aparelho. Há no máximo 10 pedidos pendentes, e os mais antigos saem.
3. **Aprovação pelo código.** Um aparelho administrador digita o código visto na tela do aparelho novo (`POST /api/devices/approve`), ou o dono do Pi roda `zoio aparelhos aprovar CÓDIGO`. Aprovar pelo código, e não clicando num item da lista, impede que um intruso com o mesmo nome seja aprovado por engano.
4. **Entrada.** `POST /api/devices/challenge` devolve um desafio aleatório de uso único, válido por 2 minutos. O aparelho assina `zoio-login\n<desafio>\n<id>` e envia para `POST /api/devices/login`. Com a assinatura válida e o aparelho aprovado, o servidor cria uma sessão: um token aleatório de 256 bits num cookie `HttpOnly; Secure; SameSite=Strict`, guardado no banco só como SHA-256. A validade se renova com o uso.
5. **Revogação.** Apaga as sessões e as inscrições de push do aparelho na hora. Um aparelho revogado não consegue se recadastrar com a mesma chave até ser excluído da lista. O último administrador não pode ser revogado pelo app, para ninguém ficar trancado para fora. O Pi sempre pode.

Exceções sem sessão: a "casca" do app (`/`, `/web/*`, `/sw.js`, manifesto), os endpoints de cadastro e entrada e os segmentos HLS do go2rtc que trazem um `id` de sessão válido. O player nativo do iPhone antigo busca esses segmentos sozinho e nem sempre manda o cookie. O `id` é aleatório (`crypto/rand`, ~47 bits) e só existe depois que um aparelho aprovado abriu o WebSocket do ao vivo.

## Portão (`esphome.py`, `gate.py`)

Uma placa ESP32 + relé com ESPHome fecha o contato da botoeira do porteiro por um tempo fixo, gravado no firmware. O Zóio fala com ela pela API nativa do ESPHome (porta 6053), cifrada com Noise e uma chave de 32 bytes, implementada direto com `cryptography`. Só conecta na hora de abrir.

`POST /api/gate/open` exige sessão de um aparelho com a permissão `gate` e uma assinatura nova do aparelho sobre `zoio-portao\n<desafio>\n<id>` (desafio de `POST /api/gate/challenge`). Há um intervalo mínimo de 5 s entre aberturas. Cada abertura vira um evento `Portao` no SQLite, com quem abriu, o resultado e uma foto do fluxo principal da câmera do portão (quadro do go2rtc), e é avisada aos outros administradores por push. Detalhes em [portao.md](portao.md).

## Segurança

- Nenhum serviço escuta fora do `127.0.0.1`. O único caminho de fora é a VPN (hoje, o Tailscale; alternativas em [acesso-remoto.md](acesso-remoto.md)).
- Cada aparelho precisa ser aprovado (seção acima). A VPN é a primeira trava, e a aprovação é a segunda.
- O portão exige, além disso, a permissão do aparelho e uma assinatura nova a cada abertura. A placa não tem página web nem OTA.
- `ZOIO_ALLOWED_USERS` restringe a logins específicos do Tailscale.
- Content-Security-Policy restrita: só scripts do próprio Zóio, sem iframes nem objetos, e conexões só com o próprio endereço.
- Pedidos com `Origin` de outro site são recusados (CSRF e WebSocket entre origens).
- Os nomes de arquivo e os horários das gravações são validados. O id do clipe é um hash, e o caminho nunca vem do cliente.
- Os serviços rodam como o usuário `zoio`, com `ProtectSystem=strict`, `ProtectHome` e `NoNewPrivileges`.
- A senha do DVR fica só em `/etc/zoio/zoio.env` (root:zoio 640) e no `go2rtc.yaml` gerado em `/var/lib/zoio` (600).
- Limitação conhecida: a URL RTSP da Xiongmai leva a senha em texto puro na rede local (é o próprio formato do DVR). Evite `&` e `#` na senha do DVR.

## Uso de recursos esperado no Pi 3B

- go2rtc: repasse puro, perto de 1–3% de CPU por fluxo. Só puxa do DVR o que alguém está assistindo, e o player para quando a câmera sai da tela.
- zoio: menos de 60 MB de RAM, CPU desprezível em repouso.
- ffmpeg: só durante o remux de um clipe (segundos, sem recodificar).
- Rede: 8 fluxos secundários somam cerca de 1–2 Mbit/s. Um principal fica em torno de 1–2 Mbit/s.

## Próximos passos

1. Fase 2: validar o portão com a placa de verdade (ligação, gravação do firmware pelo Pi e o primeiro `zoio portao testar`).
2. Miniatura nos alertas de movimento, com um quadro do go2rtc (o portão já faz isso).
3. Linha do tempo do dia por câmera, com marcações de movimento.
