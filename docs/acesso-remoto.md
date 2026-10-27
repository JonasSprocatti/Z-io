# Acesso de fora de casa: opções em aberto

> **Situação (27/10/2026):** a decisão fica para depois. Por enquanto o Zóio usa o **Tailscale** (opção A), que já está instalado no Pi. A opção B (WireGuard próprio) está descrita aqui para quando o Jonas quiser decidir.

Em qualquer opção, o Zóio exige a **aprovação de cada aparelho** (chave própria por aparelho, código para conferir e revogação a qualquer momento). Essa é a segunda trava, e ela não depende da VPN escolhida. Veja a seção "Aparelhos autorizados" do [README](../README.md).

Também em qualquer opção, **nenhuma porta TCP/HTTP fica aberta para a internet**. O app só é alcançável por dentro de uma VPN.

## Comparação

| | A. Tailscale (atual) | B. WireGuard próprio no Pi | C. Headscale |
|---|---|---|---|
| Empresa no meio | Tailscale (coordenação). O tráfego é cifrado de ponta a ponta, e ela não vê o vídeo. | Nenhuma | Nenhuma (servidor seu) |
| Porta aberta no roteador | Nenhuma | 1 porta UDP. Ela não responde a quem não tem chave e, para varreduras, parece fechada. | Depende de onde o servidor fica |
| Funciona com CGNAT da operadora | Sim | **Não** (precisaria de um servidor na nuvem como ponto de encontro) | Precisa de um servidor público |
| Aprovar aparelho na VPN | Painel do Tailscale (*Device approval*, *Tailnet Lock*) | Adicionar a chave do aparelho no Pi (QR code) | Comando no servidor Headscale |
| HTTPS para o app (necessário para notificações) | Automático (`tailscale serve`) | Certificado Let's Encrypt via DuckDNS (DNS-01), renovado sozinho | Igual à B |
| Custo | Grátis (plano pessoal) | Grátis (DuckDNS) | Servidor na nuvem (ou Oracle Free Tier) |
| Manutenção | Quase nenhuma | Baixa: IP dinâmico via DuckDNS e renovação do certificado | Média: mais um servidor para cuidar |

## A. Tailscale (atual)

O que já funciona: `tailscale serve` publica `https://techdeck.<sua-rede>.ts.net` só para aparelhos da sua rede Tailscale.

Para reforçar sem mudar nada no Zóio, no painel do Tailscale (https://login.tailscale.com/admin/settings/device-management):
- **Device approval:** todo aparelho novo fica bloqueado até você aprovar.
- **Tailnet Lock** (opcional): cada aparelho precisa ser assinado por uma chave sua, guardada nos seus aparelhos confiáveis. Nem a própria Tailscale consegue colocar um aparelho na sua rede.
- **Key expiry:** deixe ligado nos celulares e desligue só no Pi.

## B. WireGuard próprio no Pi

É o "Tailscale próprio" mais simples: o Tailscale é construído em cima do WireGuard.

### Antes de tudo: tem CGNAT?
1. Na página do roteador (`http://192.168.15.1`), anote o "IP WAN" ou "IPv4 da Internet".
2. Num aparelho da casa, abra https://ifconfig.me.
3. Se os dois forem iguais, o IP é público e a opção B funciona. Se o do roteador começar com `100.64.` a `100.127.` (ou `10.`, `192.168.`), há CGNAT. Nesse caso, peça IP público à Vivo (às vezes liberam) ou fique com a A.

### Como ficaria
```
celular ──WireGuard (UDP 51820)──► roteador ──► Pi (10.8.0.1) ──► Zóio (HTTPS) ──► DVR
```
- O Pi roda o WireGuard (pacote `wireguard-tools`, que já vem no kernel do Ubuntu) com a rede interna `10.8.0.0/24`.
- No roteador, só a porta **UDP 51820** é encaminhada para o Pi. Nenhuma porta TCP.
- **DuckDNS** (grátis) mantém um nome, por exemplo `zoio-jonas.duckdns.org`, apontando para o IP de casa, que muda de tempos em tempos.
- **Certificado HTTPS:** Let's Encrypt com validação por DNS no DuckDNS (`acme.sh --dns dns_duckdns`). O app fica em algo como `https://app.zoio-jonas.duckdns.org`, que aponta para `10.8.0.1` e só resolve para quem está na VPN. O Zóio passa a servir HTTPS direto, ou o Caddy faz isso.
- **Um aparelho = uma chave WireGuard.** Um comando no Pi (`sudo zoio vpn adicionar "Celular do Jonas"`) gera a chave, coloca o aparelho na lista e mostra um QR code para o app oficial do WireGuard (Android, iOS, Windows, Mac, Linux). Remover = `sudo zoio vpn remover ...`, que derruba o aparelho na hora.
- **Só a rede do Zóio** passa pela VPN (`AllowedIPs = 10.8.0.1/32`). O resto da internet do celular segue normal, e o celular não enxerga a rede de casa nem o DVR.

### Cuidados
- Alguns roteadores bloqueiam respostas de DNS com IP interno ("DNS rebinding"). Se o nome não resolver dentro de casa, a configuração do WireGuard pode apontar o DNS para o Pi.
- Se a Vivo trocar o modem ou mudar para CGNAT, o acesso de fora para até migrar para a A ou para um servidor na nuvem.

### Trabalho para implementar (estimativa)
1. Script de instalação do WireGuard e do DuckDNS no `deploy/` (com IP dinâmico e certificado).
2. `zoio vpn adicionar/remover/listar` com QR code no terminal.
3. Zóio servindo HTTPS com o certificado do DuckDNS (ou Caddy na frente).
4. Abrir a porta UDP no roteador, que o Jonas faz.

## C. Headscale

É um servidor de coordenação do Tailscale feito pela comunidade, com os mesmos apps do Tailscale. Ele precisa ficar acessível pela internet (HTTPS), então ou abre uma porta TCP em casa, o que contraria a ideia de não expor nada, ou roda num servidor na nuvem. Faz sentido se um dia houver muitos aparelhos ou várias casas. Para uma casa só, a B resolve com menos peças.
