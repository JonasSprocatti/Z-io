# Zóio

App para ver as câmeras de casa: ao vivo em grade, gravações do DVR e alerta de movimento no celular. Roda num Raspberry Pi 3B ao lado do DVR e só é acessível pelo Tailscale. Nada fica exposto na internet.

Funciona no Android, no iPhone, no computador e no navegador, porque é um app web instalável (PWA). Basta abrir o endereço do Pi e escolher "Adicionar à tela de início".

## Como funciona

```
 celular / PC ──Tailscale (HTTPS)──► Raspberry Pi ──rede de casa──► DVR Xiongmai
                                     │                              ├─ RTSP 554 (ao vivo)
                                     ├─ tailscale serve (HTTPS)     └─ DVRIP 34567 (gravações, alarmes)
                                     ├─ zoio      (app, API, alertas)
                                     └─ go2rtc    (ao vivo, sem recodificar)
```

- **Ao vivo:** o go2rtc pega o RTSP do DVR e entrega ao navegador por MSE/HLS, sem recodificar (o Pi 3B não aguentaria). A grade usa o fluxo secundário, e a tela cheia usa o principal.
- **Gravações:** continuam no HD do DVR. O Zóio busca os arquivos pelo protocolo DVRIP, baixa o trecho escolhido e converte para MP4 sem recodificar. Os vídeos ficam num cache no Pi.
- **Alertas:** o Zóio fica inscrito nos alarmes de movimento do próprio DVR. Cada alerta aparece na hora no app e vira uma notificação push (Web Push cifrado de ponta a ponta), com um intervalo mínimo por câmera.
- **Aparelhos autorizados:** cada aparelho cria a própria chave digital, que nunca sai dele, e só vê as câmeras depois que você o aprova. Dá para revogar qualquer um na hora.
- **Segurança:** os serviços só escutam no `127.0.0.1` do Pi. O acesso de fora passa pelo `tailscale serve`, que exige estar na sua rede Tailscale. Dá para limitar a usuários específicos com `ZOIO_ALLOWED_USERS`. A interface de administração do go2rtc nunca é exposta. As alternativas ao Tailscale (WireGuard próprio, Headscale) estão em [docs/acesso-remoto.md](docs/acesso-remoto.md).

Mais detalhes em [docs/arquitetura.md](docs/arquitetura.md).

## Instalação no Raspberry Pi

Pré-requisitos: Pi na mesma rede do DVR, com o Tailscale ligado. No painel do Tailscale, ative **MagicDNS** e **HTTPS Certificates** (em DNS).

```bash
git clone https://github.com/JonasSprocatti/Z-io.git ~/zoio
cd ~/zoio
sudo ./deploy/install.sh
```

O instalador:
1. instala ffmpeg e as dependências pelo apt;
2. cria o usuário de serviço `zoio`, o `/etc/zoio/zoio.env` e o `/var/lib/zoio`;
3. baixa o go2rtc para a arquitetura do Pi;
4. liga os serviços `zoio` e `zoio-go2rtc` no systemd;
5. publica o app em `https://<nome-do-pi>.<sua-rede>.ts.net` com `tailscale serve`.

Se existir `~/.zoio-dvr.env` (do teste anterior), o usuário e a senha do DVR são copiados de lá. Senão, edite `/etc/zoio/zoio.env`. Lá também ficam os nomes das câmeras:

```bash
sudo nano /etc/zoio/zoio.env
sudo systemctl restart zoio-go2rtc zoio
```

Para testar a conexão com o DVR (login, relógio, gravações das últimas 24h e 15 s de alarmes):

```bash
sudo zoio check
sudo zoio check --baixar   # também baixa o começo da gravação mais recente e converte para MP4
```

## Aparelhos autorizados

Só aparelhos aprovados abrem o Zóio, mesmo estando na sua rede Tailscale.

1. Abra o Zóio no aparelho. Ele cria uma chave própria (ECDSA P-256, não exportável, guardada pelo navegador) e mostra um **código**, como `K7Q2-M9XD`.
2. **Primeiro aparelho:** aprove no Pi com `sudo zoio aparelhos aprovar K7Q2-M9XD`. Ele vira administrador.
3. **Os próximos:** num aparelho administrador, abra **Ajustes › Aparelhos** e digite o código que aparece na tela do aparelho novo. Os administradores recebem um aviso quando alguém pede acesso. Marque "pode aprovar outros aparelhos" só nos seus.
4. Para tirar o acesso, use **Revogar** no app ou `sudo zoio aparelhos revogar CÓDIGO`. A sessão cai na hora e as notificações daquele aparelho param.

Outros comandos: `sudo zoio aparelhos` (lista) e `sudo zoio aparelhos excluir CÓDIGO` (apaga um aparelho revogado para ele poder pedir de novo).

Detalhes que importam:
- Depois de aprovado, o aparelho entra sozinho: ele assina um desafio com a chave dele e recebe uma sessão (cookie HttpOnly) que vale `ZOIO_SESSION_DAYS` dias sem uso.
- No iPhone, o Safari e o app da Tela de Início guardam dados separados e contam como dois aparelhos. Aprove o da Tela de Início.
- Limpar os dados do site, ou usar janela anônima, apaga a chave. Aí o aparelho precisa ser aprovado de novo.

Para atualizar: `git pull && sudo ./deploy/install.sh`.

## Ajustes recomendados no DVR

- **Fluxo secundário em H.264.** O DVR manda tudo em H.265. Safari e Chrome com aceleração de vídeo tocam H.265, mas Firefox e alguns aparelhos não. Com o secundário em H.264, a grade toca em qualquer lugar. A gravação continua em H.265.
- **Horário de verão desligado.** Senão o relógio do DVR fica 1 hora adiantado, e os horários das gravações e dos alertas saem errados.
- **IP fixo** (reserva DHCP no roteador) para o DVR e para o Pi.
- **Pi no cabo de rede**, se possível. A grade com 8 câmeras passa pelo Pi.

## Uso

- **Ao vivo:** toque numa câmera para abrir em tela cheia no fluxo principal. Os botões 1 a 4 mudam quantas câmeras aparecem por linha. Uma borda vermelha indica movimento.
- **Gravações:** escolha a câmera e o dia. Os trechos com movimento aparecem marcados. "Assistir" baixa o trecho do DVR, a cerca de 1 MB/s, e toca no app. Também dá para baixar o MP4.
- **Alertas:** histórico dos últimos 30 dias, com atalho para a gravação.
- **Ajustes:** ativa as notificações neste aparelho e escolhe quais câmeras avisam. No iPhone, primeiro adicione o Zóio à Tela de Início (iOS 16.4 ou mais novo). Nos aparelhos administradores, também lista e aprova os aparelhos.

## Desenvolvimento

```bash
cd server
pip install -e '.[test]'
pytest
```

Os testes usam um DVR simulado (`tests/fake_dvr.py`), que fala DVRIP, e um go2rtc simulado. Eles cobrem login, busca paginada, download e conversão para MP4 (conferida com o ffprobe), alarmes, Web Push, controle de acesso, o proxy do ao vivo e a aprovação de aparelhos (assinaturas, desafio de uso único, revogação e linha de comando).

## Próximas fases

- Abrir o portão pelo app (ESP32 + relé). Veja a lista de compras da fase 2.
