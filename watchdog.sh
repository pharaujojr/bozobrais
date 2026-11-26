#!/usr/bin/env bash
# Vigia os streams base do go2rtc, destrava produtor zumbi e avisa no Telegram.
#
# Só reinicia na condição em que reiniciar resolve: a câmera responde na porta
# RTSP, mas o produtor do go2rtc não recebe um byte há vários ciclos (produtor
# zumbi). Se a câmera está fora do ar, reiniciar não traz vídeo nenhum e só
# geraria uma tempestade de reconexão — nesse caso o watchdog não mexe em nada,
# apenas avisa, e deixa o go2rtc reconectar sozinho, que é o que ele já faz bem.
#
# Só olha stream base (sem o prefixo v_) que tenha consumidor: os *_hd ficam
# ociosos de propósito, o go2rtc só conecta sob demanda, e zero byte neles é
# saúde, não falha.
#
# Telegram: credenciais em data/telegram.conf (TG_TOKEN / TG_CHAT). Sem esse
# arquivo o watchdog funciona igual, só não avisa.
set -uo pipefail

GO2RTC_API=${GO2RTC_API:-http://127.0.0.1:1984/api/streams}
CONTAINER=${CONTAINER:-cam_go2rtc}
STATE=${STATE:-$(cd "$(dirname "$0")" && pwd)/data/.watchdog_state}
TG_CONF=${TG_CONF:-$(cd "$(dirname "$0")" && pwd)/data/telegram.conf}
STRIKES_MAX=${STRIKES_MAX:-3}       # ciclos parados antes de reiniciar (3 x 2min = 6min)
DOWN_STRIKES=${DOWN_STRIKES:-3}     # ciclos fora do ar antes de avisar
COOLDOWN=${COOLDOWN:-1800}          # no mínimo 30min entre restarts

log() { echo "$(date '+%F %T') $*"; }

[ -r "$TG_CONF" ] && . "$TG_CONF"

tg() {
    # Avisa no Telegram. Nunca derruba o watchdog: alerta é acessório, vigiar não é.
    [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || return 0
    local resp
    resp=$(curl -s --max-time 15 -X POST \
        "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "text=$1" \
        --data-urlencode "disable_notification=${2:-false}") || {
            log "telegram: falha de rede"; return 0; }
    case "$resp" in
        *'"ok":true'*) ;;
        *) log "telegram recusou: $(printf '%s' "$resp" | head -c 200)" ;;
    esac
}

# --- estado (arquivo chave/valor simples) ---
st_get() { grep -E "^$1 " "$STATE" 2>/dev/null | tail -1 | cut -d' ' -f2; }
st_set() { sed -i "/^$1 /d" "$STATE" 2>/dev/null; printf '%s %s\n' "$1" "$2" >> "$STATE"; }

# Duração legível a partir de segundos.
human() {
    local s=$1
    if   [ "$s" -lt 60 ]   ; then echo "${s}s"
    elif [ "$s" -lt 3600 ] ; then echo "$((s/60))min"
    else echo "$((s/3600))h$(( (s%3600)/60 ))min"; fi
}

json=$(curl -s --max-time 10 "$GO2RTC_API") || { log "go2rtc não respondeu na API"; exit 0; }

# -> "nome bytes_recv ip" por stream base ativo (com consumidor)
read -r -d '' PARSE <<'PY'
import json, sys, re
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for name, s in d.items():
    if name.startswith("v_"):
        continue
    if not (s.get("consumers") or []):
        continue
    prods = s.get("producers") or []
    total = sum(p.get("bytes_recv", 0) for p in prods)
    ip = ""
    for p in prods:
        m = re.search(r"//(?:[^@/]*@)?(\d+\.\d+\.\d+\.\d+)", p.get("url", ""))
        if m:
            ip = m.group(1)
            break
    print(name, total, ip)
PY

mapfile -t rows < <(printf '%s' "$json" | python3 -c "$PARSE")
[ ${#rows[@]} -eq 0 ] && exit 0

touch "$STATE"
now=$(date +%s)
stuck=""

for row in "${rows[@]}"; do
    read -r name bytes ip <<<"$row"
    prev=$(st_get "bytes:$name");     prev=${prev:-0}
    strikes=$(st_get "strikes:$name"); strikes=${strikes:-0}
    unreach=$(st_get "unreach:$name"); unreach=${unreach:-0}
    down=$(st_get "down:$name");       down=${down:-0}
    since=$(st_get "downsince:$name"); since=${since:-$now}

    if [ "$bytes" -gt "$prev" ]; then
        # Fluindo. Se estávamos em alerta, avisa que voltou.
        if [ "$down" = "1" ]; then
            log "$name voltou a receber depois de $(human $((now - since)))"
            tg "✅ Câmera de volta

O stream $name voltou a receber vídeo depois de $(human $((now - since))) fora."
            st_set "down:$name" 0
        fi
        strikes=0; unreach=0
    else
        # Parado. Reiniciar só resolve se a câmera estiver de fato alcançável.
        if [ -n "$ip" ] && timeout 4 bash -c "echo > /dev/tcp/$ip/554" 2>/dev/null; then
            strikes=$((strikes + 1)); unreach=0
            log "$name parado em $bytes bytes, câmera $ip responde na 554 — strike $strikes/$STRIKES_MAX"
        else
            unreach=$((unreach + 1)); strikes=0
            log "$name parado, câmera ${ip:-?} inalcançável ($unreach) — aguardando a câmera voltar"
            if [ "$unreach" -ge "$DOWN_STRIKES" ] && [ "$down" = "0" ]; then
                st_set "down:$name" 1
                st_set "downsince:$name" "$now"
                tg "🔴 Câmera fora do ar

O stream $name parou e a câmera ${ip:-?} não responde na porta 554.
Sem vídeo e sem gravação até ela voltar.

O go2rtc reconecta sozinho assim que ela voltar — te aviso quando isso acontecer."
            fi
        fi
    fi

    [ "$strikes" -ge "$STRIKES_MAX" ] && stuck="$name"
    st_set "bytes:$name" "$bytes"
    st_set "strikes:$name" "$strikes"
    st_set "unreach:$name" "$unreach"
done

[ -n "$stuck" ] || exit 0

last=$(st_get "last_restart")
if [ -n "${last:-}" ] && [ $((now - last)) -lt "$COOLDOWN" ]; then
    log "produtor travado, mas restart em cooldown ($((COOLDOWN - now + last))s restantes)"
    exit 0
fi

log "produtor travado com a câmera no ar — reiniciando $CONTAINER"
if docker restart "$CONTAINER" >/dev/null 2>&1; then
    log "$CONTAINER reiniciado"
    tg "⚠️ Stream travado — reiniciei o go2rtc

O stream $stuck ficou $((STRIKES_MAX * 2)) min sem receber vídeo, mas a câmera estava
respondendo na 554 (produtor travado). Reiniciei o $CONTAINER para destravar.

Se chegar outro aviso desses em seguida, o problema não é o go2rtc."
else
    log "FALHA ao reiniciar $CONTAINER"
    tg "❗ Stream travado e NÃO consegui reiniciar

O stream $stuck está sem vídeo e o comando docker restart $CONTAINER falhou.
Precisa de olho manual no servidor."
fi
sed -i '/^strikes:/d' "$STATE" 2>/dev/null
st_set "last_restart" "$now"
