#!/usr/bin/env bash
# Configura o alerta do watchdog no Telegram.
#   ./setup-telegram.sh 8123456789:AAH...
# Descobre o chat id sozinho a partir da última mensagem que você mandou ao bot,
# grava data/telegram.conf (só o dono lê) e manda uma mensagem de teste.
set -uo pipefail

CONF=${CONF:-$(cd "$(dirname "$0")" && pwd)/data/telegram.conf}
TOKEN=${1:-}

if [ -z "$TOKEN" ]; then
    echo "uso: $0 <token-do-BotFather>" >&2
    exit 1
fi

echo "→ conferindo o token…"
me=$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe")
case "$me" in
    *'"ok":true'*) ;;
    *) echo "Token recusado pelo Telegram: $me" >&2
       echo "Confira se copiou o token inteiro do BotFather (tem dois-pontos no meio)." >&2
       exit 1 ;;
esac
uname=$(printf '%s' "$me" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')
echo "  bot @$uname"

echo "→ procurando sua conversa com o bot…"
upd=$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getUpdates")
chat=$(printf '%s' "$upd" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for u in reversed(d.get("result") or []):
    for k in ("message", "channel_post", "my_chat_member"):
        c = (u.get(k) or {}).get("chat")
        if c:
            print(c["id"], (c.get("title") or c.get("first_name") or "").strip())
            sys.exit(0)
')

if [ -z "$chat" ]; then
    cat >&2 <<EOF

Nenhuma conversa encontrada. O Telegram só entrega o chat id depois que VOCÊ
falar com o bot primeiro:

  1. abra o Telegram e procure @$uname
  2. clique em Start (ou mande qualquer mensagem, um "oi" serve)
  3. rode este comando de novo

EOF
    exit 1
fi

chat_id=${chat%% *}
chat_nome=${chat#* }
echo "  chat $chat_id ($chat_nome)"

umask 077
printf 'TG_TOKEN=%s\nTG_CHAT=%s\n' "$TOKEN" "$chat_id" > "$CONF"
chmod 600 "$CONF"
echo "→ gravado em $CONF (permissão 600: o token é a senha do bot)"

echo "→ mandando mensagem de teste…"
resp=$(curl -s --max-time 15 -X POST \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${chat_id}" \
    --data-urlencode "text=🎥 Alerta das câmeras configurado.

A partir de agora eu aviso aqui quando a câmera sair do ar, quando ela voltar,
e quando o stream travar e eu reiniciar o go2rtc sozinho.

Se estiver tudo bem, você não recebe mais nada.")
case "$resp" in
    *'"ok":true'*) echo "  enviada — confere no seu Telegram." ;;
    *) echo "  o Telegram recusou o envio: $resp" >&2; exit 1 ;;
esac
