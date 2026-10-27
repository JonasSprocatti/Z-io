#!/usr/bin/env bash
# Instala ou atualiza o Zóio no Raspberry Pi. Rode de dentro do repositório:
#   sudo ./deploy/install.sh
# Pode rodar de novo a qualquer momento; não apaga a configuração nem os dados.
set -euo pipefail

GO2RTC_VERSION="${GO2RTC_VERSION:-1.9.9}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Rode com sudo: sudo $0" >&2
  exit 1
fi

echo "==> Pacotes do sistema"
apt-get update -qq
apt-get install -y -qq python3-venv python3-aiohttp python3-cryptography ffmpeg curl >/dev/null

echo "==> Usuário e pastas"
id zoio &>/dev/null || useradd --system --home /var/lib/zoio --shell /usr/sbin/nologin zoio
install -d -o root -g zoio -m 750 /etc/zoio
install -d -o zoio -g zoio -m 750 /var/lib/zoio
install -d -m 755 /opt/zoio/bin

echo "==> Servidor do Zóio"
[[ -x /opt/zoio/venv/bin/python ]] || python3 -m venv --system-site-packages /opt/zoio/venv
# As dependências vêm do apt (já compiladas para o Pi); o pip só instala o Zóio.
/opt/zoio/venv/bin/pip install -q --no-deps --upgrade "$REPO/server"
/opt/zoio/venv/bin/python -c "import aiohttp, cryptography; print('    aiohttp', aiohttp.__version__)"

echo "==> go2rtc $GO2RTC_VERSION"
case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64 ;;
  armv7l|armv6l) ARCH=arm ;;
  x86_64) ARCH=amd64 ;;
  *) echo "Arquitetura não suportada: $(uname -m)" >&2; exit 1 ;;
esac
if ! /opt/zoio/bin/go2rtc -version 2>/dev/null | grep -q "$GO2RTC_VERSION"; then
  curl -fsSL -o /opt/zoio/bin/go2rtc.new \
    "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${ARCH}"
  chmod 755 /opt/zoio/bin/go2rtc.new
  mv /opt/zoio/bin/go2rtc.new /opt/zoio/bin/go2rtc
  sha256sum /opt/zoio/bin/go2rtc
fi

echo "==> Configuração"
if [[ ! -f /etc/zoio/zoio.env ]]; then
  install -o root -g zoio -m 640 "$REPO/deploy/zoio.env.example" /etc/zoio/zoio.env
  # Aproveita as credenciais do teste anterior (~/.zoio-dvr.env), se existirem.
  OLD="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/.zoio-dvr.env"
  if [[ -f "$OLD" ]]; then
    for key in DVR_HOST DVR_USER DVR_PASS; do
      value="$(bash -c "set -a; . '$OLD'; printf '%s' \"\${$key:-}\"")"
      if [[ -n "$value" ]]; then
        python3 - "$key" "$value" <<'PY'
import sys, re, shlex
key, value = sys.argv[1], sys.argv[2]
path = "/etc/zoio/zoio.env"
text = open(path, encoding="utf-8").read()
text = re.sub(rf"^{key}=.*$", f"{key}={shlex.quote(value)}", text, count=1, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PY
      fi
    done
    echo "    credenciais copiadas de $OLD"
  else
    echo "    edite /etc/zoio/zoio.env e coloque a senha do DVR"
  fi
fi

echo "==> Atalho de linha de comando (sudo zoio ...)"
cat > /usr/local/bin/zoio <<'SH'
#!/bin/sh
# Atalho para a linha de comando do Zóio. Roda como o usuário do serviço.
# Exemplos: sudo zoio aparelhos | sudo zoio aparelhos aprovar ABCD-EFGH | sudo zoio check
exec sudo -u zoio env ZOIO_ENV_FILE=/etc/zoio/zoio.env /opt/zoio/venv/bin/python -m zoio "$@"
SH
chmod 755 /usr/local/bin/zoio

echo "==> Serviços"
install -m 644 "$REPO/deploy/zoio.service" /etc/systemd/system/zoio.service
install -m 644 "$REPO/deploy/zoio-go2rtc.service" /etc/systemd/system/zoio-go2rtc.service
systemctl daemon-reload
systemctl enable -q zoio-go2rtc zoio
systemctl restart zoio-go2rtc zoio

echo "==> Acesso pelo Tailscale (só aparelhos da sua rede Tailscale)"
if command -v tailscale >/dev/null; then
  # Se o Serve ainda não foi liberado na conta, o tailscale mostra um link e
  # fica esperando. O timeout evita que o instalador trave nesse ponto.
  if SERVE_OUT="$(timeout 20 tailscale serve --bg --https=443 http://127.0.0.1:8080 2>&1)"; then
    NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    echo "    Abra https://$NAME no celular ou no computador (com o Tailscale ligado)."
  else
    echo "    Não consegui ativar o 'tailscale serve'. O Tailscale respondeu:"
    printf '%s\n' "$SERVE_OUT" | grep -v '^\s*$' | tail -n 6 | sed 's/^/      /' || true
    echo "    Libere o Serve pelo link acima (ou ative MagicDNS e HTTPS em https://login.tailscale.com/admin/dns)"
    echo "    e rode: sudo tailscale serve --bg --https=443 http://127.0.0.1:8080"
  fi
else
  echo "    Tailscale não encontrado."
fi

sleep 2
systemctl --no-pager --lines=0 status zoio zoio-go2rtc | grep -E "●|Active:" || true
echo "Pronto."
echo "  Testar o DVR:            sudo zoio check"
echo "  Ver aparelhos:           sudo zoio aparelhos"
echo "  Aprovar o 1º aparelho:   abra o Zóio nele e rode: sudo zoio aparelhos aprovar CÓDIGO"
