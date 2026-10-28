# Porteiro do Zóio (fase 2)

Uma placa ESP32-S3 no portão faz o papel do porteiro: avisa quando tocam a campainha, abre a fechadura do portão de pedestre e aciona os motores das duas garagens, que ficam ao lado. Tudo pelo Wi-Fi de casa, sem nuvem.

```
 app (aparelho com permissão) ──Tailscale──► Pi (zoio) ──Wi-Fi de casa──► ESP32-S3 no portão
                                              │  API nativa do ESPHome,       ├─ relé 1 ─► fechadura do portão
                                              │  cifrada (porta 6053),        ├─ relé 2 ─► botoeira da garagem 1
                                              │  conexão sempre aberta        ├─ relé 3 ─► botoeira da garagem 2
                                              │                               ├─ sensores magnéticos das garagens
                                              └─ fotos e vídeo (go2rtc)       └─ botão da campainha
```

Esta é a parte 1. A parte 2 traz a conversa: falar e ouvir pelo celular, no estilo "aperte para falar", com microfone e alto-falante na mesma placa.

## Como usar

- O botão **Portaria** aparece no topo do app só nos aparelhos com permissão. Ele abre uma tela com o ao vivo da câmera da porta escolhida e uma aba para cada porta: **Portão**, **Garagem 1** e **Garagem 2** (os nomes vêm do `ZOIO_GARAGE_NAMES`).
- Os botões de comando precisam ser **segurados** até completar o círculo (pouco mais de 1 segundo). Um toque sem querer não faz nada.
- **Portão:** segurar **abre** a fechadura.
- **Garagens:** a aba mostra se a garagem está aberta ou fechada, pelo sensor magnético. O botão muda conforme o estado:
  - fechada: **Abrir**;
  - aberta: **Fechar**, só com o vídeo daquela garagem tocando na tela;
  - em movimento (o tempo do `ZOIO_GARAGE_TRAVEL` depois do comando): só o botão vermelho **Parar**, que funciona com um toque;
  - depois de parar no meio, ou sem sensor: **Acionar**, que também exige o vídeo, porque o motor pode tanto abrir quanto fechar.
- **Campainha:** quem tem a permissão de portaria recebe uma notificação urgente ("Tocaram no portão às 14:32"), que vibra e fica na tela até ser tocada. Ela abre a portaria já no portão. Com o app aberto, a portaria abre sozinha com o aviso.
- Cada toque da campainha, cada comando e cada garagem que abriu ou fechou por fora (pelo controle remoto, por exemplo) entram em **Alertas**, com uma foto da câmera daquela porta. Os comandos registram quem mandou, e os outros administradores recebem uma notificação.
- Se a placa sair do ar, a portaria mostra "A placa do porteiro está desconectada" e o Zóio tenta reconectar sozinho.

## Segurança

- **Duas travas antes da portaria.** Só chega no Zóio quem está na sua rede Tailscale *e* usa um aparelho aprovado. A portaria ainda exige a permissão de portaria naquele aparelho. Sem a aprovação de aparelhos ligada (`ZOIO_DEVICE_APPROVAL=1`), a portaria fica desligada.
- **Assinatura a cada comando.** Não basta a sessão do navegador: para cada comando, o aparelho assina um desafio novo junto com a porta e a ação (`zoio-portaria\n<desafio>\n<id>\n<porta>\n<ação>`) com a chave dele, que não sai do aparelho. Um cookie roubado não abre nada, e uma assinatura para abrir a garagem 1 não serve para fechar a garagem 2.
- **Regras das garagens no servidor.** O Pi confere o estado antes de mandar o comando, e o app só repete as mesmas regras: fechar só com o sensor dizendo "aberta", durante o movimento só parar, e entre dois comandos da mesma porta pelo menos 5 segundos (parar não espera).
- **Sem fotocélula.** As fotocélulas são opcionais e não mudam nada no software. Sem elas, a segurança ao fechar fica por conta de ver a garagem ao vivo e do botão Parar. Vale conferir se a central tem detecção de obstáculo ("sensibilidade").
- **Placa fechada.** Na placa só existe a API nativa do ESPHome, cifrada e autenticada com uma chave de 32 bytes (`ZOIO_GATE_KEY`). Não tem página web, atualização pela rede (OTA), nem ponto de acesso de emergência. Para regravar, ela volta para o USB do Pi.
- **Pulsos fixos na placa.** O tempo de cada relé (1 s na fechadura, 700 ms na botoeira) está gravado no firmware. Nenhum comando consegue deixar um contato fechado, e apertos repetidos durante o pulso são ignorados. Ao ligar, a placa desliga os três relés antes de tudo.
- **Sem nuvem.** Nada de Tuya, eWeLink ou Home Assistant Cloud. A placa só fala com a rede de casa.

## Permissões

- Ao aprovar um aparelho, marque **pode usar a portaria (portão, garagens e campainha)** se for o caso. Os administradores ganham a permissão por padrão.
- Em **Ajustes › Aparelhos**, os botões **Liberar portaria** e **Bloquear portaria** mudam a permissão de quem já está aprovado.
- No Pi:

```bash
sudo zoio aparelhos portao CÓDIGO        # libera
sudo zoio aparelhos portao CÓDIGO --nao  # bloqueia
sudo zoio aparelhos                      # lista; quem pode usar aparece com "(portaria)"
```

Revogar um aparelho tira a permissão junto.

## Peças

Lista completa e preços em `fase-2/lista-compras-portao.md` (arquivos do projeto): ESP32-S3 DevKitC-1 (N16R8), módulo relé de 4 canais com optoacoplador, o botão da campainha, regulador 12 V → 5 V, fonte 12 V com bateria e caixa hermética, mais o que a opção das garagens pedir (abaixo). O microfone, o amplificador e o alto-falante entram na parte 2, mas os pinos já estão reservados.

Para montar sem solda: ESP32-S3 e módulos com os pinos já soldados, uma placa de expansão com bornes para o ESP32-S3 de 44 pinos, jumpers fêmea-fêmea e conectores de alavanca para dividir 5 V e GND.

## Garagens: três jeitos de acionar, dois de saber o estado

Para o Zóio é tudo igual: um botão `garagem_N` por garagem e, se houver, um sensor `garagem_N_aberta`. A escolha fica no fim do `porteiro.yaml`, em `packages:`.

| Acionamento | Pacote | Quando usar |
|---|---|---|
| Fio até a central | `porteiro/garagens-rele.yaml` (padrão) | Qualquer central. Relés 2 e 3 na entrada de botoeira (BOT e GND/COM), com um par de fios até cada central. |
| Controle extra | `porteiro/garagens-rele.yaml` | Central com controle rolling code e sem fio até ela. Um controle original fica na caixa, cadastrado na central, e os relés 2 e 3 apertam os botões dele (precisa soldar dois fios em cada botão). |
| Rádio, código fixo | `porteiro/garagens-radio.yaml` | Controle de código fixo (HT6P20, EV1527). A placa transmite o mesmo código, sem fio até a central. Quem estiver perto com o equipamento certo consegue copiar um código fixo, como já acontece com o controle de vocês. |

| Estado | Pacote | Como |
|---|---|---|
| Sensor com fio | `porteiro/sensores-fio.yaml` (padrão) | Sensor magnético de alarme, um par de fios até a placa. |
| Sensor sem fio | `porteiro/sensores-radio.yaml` | Sensor de abertura 433 MHz a pilha que mande um código ao abrir e outro ao fechar. Depois que a placa reinicia, o estado volta na próxima vez que a garagem abrir ou fechar. |

Sem sensor, a garagem aparece no app só com **Acionar**.

## Ligação

Esquema passo a passo, com as opções: [Montagem do Porteiro](https://claude.ai/artifact/FWdz2iXjixk4tvX1NonjBW). *Fechadura e centrais a confirmar com as fotos.*

Tudo vai na fileira esquerda do ESP32-S3 DevKitC-1 (olhando por cima, com o USB para baixo). Vale o nome impresso ao lado do pino.

| ESP32-S3 | Vai em |
|---|---|
| 3V3 | microfone (VDD) e receptor de rádio (VCC) |
| GPIO4, GPIO5, GPIO6 | microfone INMP441: SCK, WS, SD (L/R no GND). Parte 2 |
| GPIO7, GPIO15, GPIO16 | amplificador MAX98357A: BCLK, LRC, DIN (VIN no 5V). Parte 2 |
| GPIO17 | botão da campainha (o outro fio no GND) |
| GPIO8 | DATA do receptor 433 MHz RXB6 (VCC no 3V3), se usar rádio |
| GPIO9 | DATA do transmissor 433 MHz STX882 (VCC no 5V), na opção rádio |
| GPIO10 | IN1 do relé: fechadura do portão |
| GPIO11, GPIO12 | IN2 e IN3 do relé: garagens 1 e 2 (fio até a central ou controle extra) |
| GPIO13, GPIO14 | sensores magnéticos com fio das garagens 1 e 2 (o outro fio no GND) |
| 5V | saída de 5 V do regulador; também VCC do relé, VIN do amplificador e VCC do transmissor |
| GND | tudo que é GND, junto com o negativo da fonte |

- **Relés:** VCC no 5V. Se o módulo tiver jumper de nível (H/L), deixe em L. Se tiver o jumper JD-VCC, tire o jumper, ligue JD-VCC no 5V e VCC no 3V3. A maioria aciona com nível baixo (`rele_invertido: "true"`); se o seu acionar com nível alto, mude para `"false"`.
- **Fechadura:** COM e NA do relé 1 junto com o botão de abrir do porteiro, nunca na linha de 2 fios do interfone. Se a fechadura for ligada direto na fonte de 12 V: 12 V + no COM, NA no + da fechadura e um diodo 1N4007 nos terminais dela (faixa no +). Eletroímã usa o NF e um pulso mais longo.
- **Sensores com fio:** com a garagem fechada, o ímã fica encostado e o contato fecha. Se o seu sensor fizer o contrário, mude `sensor_invertido` para `"true"`.
- **Antenas de 433 MHz:** um fio rígido de 17,3 cm em cada módulo de rádio.

Os tempos (`pulso_portao` e `pulso_garagem`) e essas opções ficam no topo de [`deploy/esphome/porteiro.yaml`](../deploy/esphome/porteiro.yaml). Os nomes dos botões e sensores não podem mudar, porque são eles que o Zóio procura: `abrir_portao`, `garagem_1`, `garagem_2`, `garagem_1_aberta`, `garagem_2_aberta`, `campainha` e `sinal_wifi`.

## Programação da placa

O firmware é o [`deploy/esphome/porteiro.yaml`](../deploy/esphome/porteiro.yaml) (ESPHome 2026.10), com os pacotes de `deploy/esphome/porteiro/`. O GitHub compila as duas variantes (com fio e com rádio) a cada mudança, com segredos de mentira (`.github/workflows/firmware.yml`). A senha do Wi-Fi, a chave e os códigos de rádio ficam num `secrets.yaml` que só existe no Pi:

```yaml
wifi_ssid: "nome-da-rede"
wifi_password: "..."
api_key: "<saída do zoio portao chave>"
# só na opção rádio (saída do zoio portao radio):
radio_protocolo: "6"
radio_garagem_1: "0110..."
radio_garagem_2: "0110..."
# só com sensores sem fio:
sensor_protocolo: "1"
sensor_garagem_1_abriu: "1010..."
sensor_garagem_1_fechou: "1010..."
sensor_garagem_2_abriu: "1010..."
sensor_garagem_2_fechou: "1010..."
```

Plano para gravar (a validar quando a placa chegar): compilar e gravar no próprio Pi, com a placa no USB, para os segredos não saírem de casa. O ESPHome atual pede Python 3.12, então ele vai num ambiente separado do Zóio. A primeira compilação no Pi 3B é lenta, mas só acontece uma vez.

Depois de gravada, reserve um IP fixo para a placa no roteador (o nome dela na rede é `zoio-porteiro`).

### Aprender os códigos do rádio

Com o receptor ligado, a placa escreve no log cada código de 433 MHz que escuta. O Zóio mostra esses códigos, sem cabo:

```bash
sudo zoio portao radio              # escuta por 60 s (--segundos para mudar)
```

Aperte cada botão do controle perto da placa, um de cada vez, e abra e feche cada garagem que tiver sensor sem fio. Cada código novo aparece uma vez, com o protocolo. Anote de onde veio cada um, ponha no `secrets.yaml` e grave a placa de novo. Na primeira gravação, sem os códigos ainda, use a variante com fio; o receptor já funciona nela.

## Configuração no Zóio

```bash
zoio portao chave                     # gera uma chave nova (a mesma vai no secrets.yaml da placa)
sudo nano /etc/zoio/zoio.env          # ZOIO_GATE_HOST, ZOIO_GATE_KEY, câmeras e nomes
sudo systemctl restart zoio
sudo zoio portao testar               # conecta na placa e lista portas, sensores e campainha, sem acionar nada
sudo zoio portao abrir                # abre o portão de verdade (fica registrado como "Pi")
sudo zoio portao acionar garagem_1    # um pulso na botoeira da garagem 1 (abre, para ou fecha)
sudo zoio portao radio                # mostra os códigos de rádio que a placa escuta (para aprender o controle)
```

| Variável | Para quê |
|---|---|
| `ZOIO_GATE_HOST` | IP da placa na rede de casa |
| `ZOIO_GATE_KEY` | chave da API (base64, 32 bytes) |
| `ZOIO_GATE_CAMERA` | câmera do portão: ao vivo na portaria e foto da campainha e de cada abertura |
| `ZOIO_GARAGE_CAMERAS` | câmeras das garagens, na ordem (ex.: `2,3`). Sem câmera, a garagem não fecha pelo app |
| `ZOIO_GARAGE_NAMES` | nomes das garagens no app, na ordem (padrão `Garagem 1,Garagem 2`) |
| `ZOIO_GARAGE_TRAVEL` | segundos que o motor leva para abrir ou fechar (padrão 30); é quanto tempo o Parar fica na tela |
| `ZOIO_GATE_PORT` | porta da API (padrão 6053) |
| `ZOIO_GATE_NAME` | nome esperado da placa (padrão `zoio-porteiro`); outra placa no mesmo IP é recusada |

Em **Ajustes › Servidor**, os administradores veem se a placa está conectada, o **Wi-Fi do porteiro** (bom a partir de -65 dBm, fraco até -75 dBm, ruim abaixo disso) e o botão **Testar placa do porteiro**, que faz o mesmo que `zoio portao testar`. Com o Wi-Fi fraco, os comandos ainda chegam, mas a conversa da parte 2 vai picotar: aí vale um repetidor perto do portão.

## Como o Zóio fala com a placa (`esphome.py`, `porteiro.py`)

O Zóio implementa só o pedaço da API nativa do ESPHome que precisa, direto com `cryptography` (o `aioesphomeapi` não está no apt do Pi):

1. Handshake `Noise_NNpsk0_25519_ChaChaPoly_SHA256` com a chave da placa. Chave errada: a placa responde "Handshake MAC failure", e o Zóio mostra "a placa recusou a chave".
2. `HelloRequest`, para conferir o nome da placa.
3. `ListEntitiesRequest`, para achar os botões e os sensores pelo `object_id`.
4. `SubscribeStatesRequest`. A partir daí a placa manda cada mudança dos sensores (campainha, garagens e sinal do Wi-Fi) na hora.
5. Para cada comando, `ButtonCommandRequest` e depois um `Ping`. A resposta ao ping só chega depois que a placa tratou o comando, então ela confirma que o botão foi apertado.

A conexão fica aberta o tempo todo. O Zóio manda um ping a cada 20 segundos e derruba a conexão se a placa ficar 50 segundos calada. Depois disso, tenta de novo com espera crescente (de 2 s até 1 minuto). As portas conhecidas ficam guardadas, e a portaria continua aparecendo, como desconectada, mesmo se o Pi reiniciar com a placa fora do ar.

Os eventos vão para o SQLite: `Campainha` (Tocou), `Portao` (Aberto) e `Garagem` (Abrindo, Fechando, Parado, Acionado, ou Abriu e Fechou quando o sensor muda sem comando do Zóio), ou `Falhou` quando o comando não chegou na placa. As fotos ficam em `/var/lib/zoio/portao/`, com a mesma retenção dos alertas (30 dias). A campainha só avisa uma vez a cada 15 segundos.

Nos testes, uma placa simulada (`tests/fake_esphome.py`) faz o lado da placa com a biblioteca `noise`, independente do código do Zóio.
