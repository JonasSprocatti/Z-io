# Portão (fase 2)

Um botão no Zóio abre o portão da rua. Uma placa ESP32 com um módulo relé fica perto do porteiro e fecha o contato da botoeira por 1 segundo, como se alguém apertasse o botão de abrir.

```
 app (aparelho com permissão) ──Tailscale──► Pi (zoio) ──Wi-Fi de casa──► ESP32 + relé ──► botoeira do porteiro ──► fechadura
                                              │  API nativa do ESPHome, cifrada (porta 6053)
                                              └─ foto da câmera do portão (go2rtc)
```

## Como usar

- O botão **Portão** aparece no topo do app só nos aparelhos com permissão.
- Ele abre uma tela com o ao vivo da câmera do portão e um botão redondo. É preciso **segurar** até completar o círculo (pouco mais de 1 segundo). Um toque sem querer não abre.
- Cada abertura entra em **Alertas**, com quem abriu, se deu certo e uma foto da câmera do portão tirada na mesma hora. Os outros administradores recebem uma notificação.

## Segurança

- **Duas travas antes do portão.** Só chega no Zóio quem está na sua rede Tailscale *e* usa um aparelho aprovado. Abrir o portão ainda exige a permissão "portão" naquele aparelho. Sem a aprovação de aparelhos ligada (`ZOIO_DEVICE_APPROVAL=1`), o portão fica desligado.
- **Assinatura a cada abertura.** Não basta a sessão do navegador: para cada abertura, o aparelho assina um desafio novo (`zoio-portao\n<desafio>\n<id>`) com a chave dele, que não sai do aparelho. Um cookie roubado não abre o portão.
- **Placa fechada.** Na placa só existe a API nativa do ESPHome, cifrada e autenticada com uma chave de 32 bytes (`ZOIO_GATE_KEY`). Não tem página web, atualização pela rede (OTA), nem ponto de acesso de emergência. Para regravar, ela volta para o USB do Pi.
- **Pulso fixo na placa.** O tempo do relé (1 s) está gravado no firmware. Nenhum comando consegue deixar o contato fechado. Apertos repetidos durante o pulso são ignorados, e o Zóio também espera 5 segundos entre duas aberturas.
- **Sem nuvem.** Nada de Tuya, eWeLink ou Home Assistant Cloud. A placa só fala com a rede de casa, e o Zóio só conecta nela na hora de abrir.

## Permissões

- Ao aprovar um aparelho, marque **pode abrir o portão** se for o caso. Os administradores ganham a permissão por padrão.
- Em **Ajustes › Aparelhos**, os botões **Liberar portão** e **Bloquear portão** mudam a permissão de quem já está aprovado.
- No Pi:

```bash
sudo zoio aparelhos portao CÓDIGO        # libera
sudo zoio aparelhos portao CÓDIGO --nao  # bloqueia
sudo zoio aparelhos                      # lista; quem pode abrir aparece com "(portão)"
```

Revogar um aparelho tira a permissão junto.

## Peças

Lista completa e preços em `fase-2/lista-compras-portao.md` (arquivos do projeto): ESP32 DevKit, módulo relé de 1 canal com optoacoplador que aciona com 3,3 V, jumpers, fonte 5 V com cabo USB de dados, caixinha e um par de fios até a botoeira. Uma placa "ESP32 relay 1 canal" com o relé já integrado também serve.

## Ligação

*A confirmar com as fotos do porteiro e da fechadura.*

A ideia geral, que vale para a maioria dos porteiros com fechadura elétrica:

| ESP32 | Módulo relé |
|---|---|
| 5V (VIN) | VCC |
| GND | GND |
| GPIO26 | IN |

Os bornes **COM** e **NA** (normalmente aberto) do relé vão em paralelo com o botão de abrir do porteiro (a botoeira), nunca na linha de 2 fios do interfone. O ESP32 é alimentado pela fonte USB.

Se o módulo acionar com nível alto, mude `rele_invertido` para `"false"` em `deploy/esphome/zoio-portao.yaml`. Outro pino é só mudar `rele_pino`.

## Programação da placa

O firmware está em [`deploy/esphome/zoio-portao.yaml`](../deploy/esphome/zoio-portao.yaml) (ESPHome 2026.10, configuração validada). A senha do Wi-Fi e a chave ficam num `secrets.yaml` que só existe no Pi:

```yaml
wifi_ssid: "nome-da-rede"
wifi_password: "..."
api_key: "<saída do zoio portao chave>"
```

Plano para gravar (a validar quando a placa chegar): compilar e gravar no próprio Pi, com a placa no USB, para os segredos não saírem de casa. O ESPHome atual pede Python 3.12, então ele vai num ambiente separado do Zóio. A primeira compilação no Pi 3B é lenta, mas só acontece uma vez.

Depois de gravada, reserve um IP fixo para a placa no roteador (o nome dela na rede é `zoio-portao`).

## Configuração no Zóio

```bash
zoio portao chave                # gera uma chave nova (a mesma vai no secrets.yaml da placa)
sudo nano /etc/zoio/zoio.env     # ZOIO_GATE_HOST, ZOIO_GATE_KEY, ZOIO_GATE_CAMERA
sudo systemctl restart zoio
sudo zoio portao testar          # conecta na placa e confere o botão, sem abrir
sudo zoio portao abrir           # abre de verdade (fica registrado como "Pi")
```

| Variável | Para quê |
|---|---|
| `ZOIO_GATE_HOST` | IP da placa na rede de casa |
| `ZOIO_GATE_KEY` | chave da API (base64, 32 bytes) |
| `ZOIO_GATE_CAMERA` | câmera do portão: ao vivo na tela de abrir e foto de cada abertura |
| `ZOIO_GATE_PORT` | porta da API (padrão 6053) |
| `ZOIO_GATE_NAME` | nome esperado da placa (padrão `zoio-portao`); outra placa no mesmo IP é recusada |
| `ZOIO_GATE_BUTTON` | botão a apertar (padrão `abrir_portao`; se a placa tiver um botão só, ele é usado) |

Em **Ajustes › Servidor**, os administradores veem o estado do portão e o botão **Testar placa do portão**, que faz o mesmo que `zoio portao testar`.

## Como o Zóio fala com a placa (`esphome.py`, `gate.py`)

O Zóio implementa só o pedaço da API nativa do ESPHome que precisa, direto com `cryptography` (o `aioesphomeapi` não está no apt do Pi):

1. Handshake `Noise_NNpsk0_25519_ChaChaPoly_SHA256` com a chave da placa. Chave errada: a placa responde "Handshake MAC failure", e o Zóio mostra "a placa recusou a chave".
2. `HelloRequest`, para conferir o nome da placa.
3. `ListEntitiesRequest`, para achar o botão pelo `object_id`.
4. `ButtonCommandRequest` e depois um `Ping`. A resposta ao ping só chega depois que a placa tratou o comando, então ela confirma que o botão foi apertado.

Enquanto isso, o Zóio pede um quadro do fluxo principal da câmera do portão ao go2rtc. O evento (tipo `Portao`, estado `Aberto` ou `Falhou`, quem abriu e se tem foto) vai para o SQLite. A foto fica em `/var/lib/zoio/portao/`, com a mesma retenção dos alertas (30 dias).

Nos testes, uma placa simulada (`tests/fake_esphome.py`) faz o lado da placa com a biblioteca `noise`, independente do código do Zóio.
