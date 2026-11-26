"""
BozoBráis — API multi-câmera (XiongMai / DVRIP + genérica RTSP).

Pipeline ao vivo (decodificação e codificação na GPU, NVDEC -> NVENC):

    câmera --dvrip--> go2rtc (<cam>_sub / <cam>_main)
                         |
                         +-- v_<id>_hd   main -> recorte -> 720p  h264_nvenc
                         +-- v_<id>_lo   sub  -> recorte -> 360p  h264_nvenc  (miniaturas)
                         +-- v_<id>_rec  sub  -> sensor inteiro   h264_nvenc  (só quem grava)
                         |
       MSE / WebRTC <----+  direto do go2rtc, proxiado pela API na mesma origem
                         |
        MediaMTX <-------+  sourceOnDemand, só remux (copy):
           live_<id> / prev_<id> -> LL-HLS      cam_<id> -> gravação em disco

O recorte e o redimensionamento acontecem dentro do decodificador (hevc_cuvid),
então o quadro nunca desce para a CPU: dividir uma câmera multi-sensor em duas
custa praticamente nada.
"""
import os, re, socket, struct, json, hashlib, hmac, secrets, threading, subprocess, time, base64, asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, Query, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ---------------- Config global ----------------
API_PORT     = int(os.getenv("API_PORT", "8091"))
RTSP_PORT    = int(os.getenv("RTSP_PORT", "8554"))
HLS_PORT     = int(os.getenv("HLS_PORT", "8888"))
WEBRTC_PORT  = int(os.getenv("WEBRTC_PORT", "8889"))
MTX_API_PORT = int(os.getenv("MTX_API_PORT", "9997"))
MTX_URL      = f"http://127.0.0.1:{MTX_API_PORT}"
GO2RTC_HOST  = os.getenv("GO2RTC_HOST", "127.0.0.1:1984")
GO2RTC_API   = f"http://{GO2RTC_HOST}"
GO2RTC_RTSP  = os.getenv("GO2RTC_RTSP", "rtsp://127.0.0.1:8556").rstrip("/")

REC_DIR      = Path("/recordings")
DATA_DIR     = Path("/data")
CAMERAS_FILE = DATA_DIR / "cameras.json"

MAX_BYTES    = int(float(os.getenv("MAX_STORAGE_GB", "220")) * 1_000_000_000)
TRIGGER_PCT  = float(os.getenv("PRUNE_TRIGGER_PCT", "95")) / 100.0
TARGET_PCT   = max(0.5, TRIGGER_PCT - 0.10)
API_KEY      = os.getenv("API_KEY", "").strip()          # senha
AUTH_USER    = os.getenv("AUTH_USER", "admin")           # usuário do login (site)
# Bases públicas p/ acesso remoto (cloudflared). Vazio = usa host:porta local (LAN).
PUBLIC_HLS_BASE    = os.getenv("PUBLIC_HLS_BASE", "").rstrip("/")
PUBLIC_WEBRTC_BASE = os.getenv("PUBLIC_WEBRTC_BASE", "").rstrip("/")
WEB_DIR      = Path(__file__).parent / "web"

# Aceleração por GPU nos streams ao vivo (o ffmpeg quem roda é o do go2rtc).
# Com USE_GPU=0 tudo cai para libx264/CPU sem outra mudança.
USE_GPU      = os.getenv("USE_GPU", "1") not in ("0", "false", "no", "")

def _local_nvenc():
    """A reprodução de gravações roda no ffmpeg DESTE container — testa de fato."""
    if not USE_GPU:
        return False
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "h264_nvenc",
                            "-f", "null", "-"], capture_output=True, timeout=25)
        return p.returncode == 0
    except Exception:
        return False

LOCAL_NVENC = None   # resolvido no startup (o teste custa ~1 s)

# Perfis ao vivo: (largura_max, altura_max, bitrate)
PROFILE_HD  = (1280, 720, "2500k")
PROFILE_LO  = (640, 360, "500k")
PROFILE_REC = (1280, 1440, "900k")   # sensor inteiro, sem recorte

# Nenhuma câmera pré-cadastrada: adicione pelo site (botão "+", que auto-detecta
# câmeras XiongMai) ou por POST /api/cameras.
SEED = []

# ======================= Registro de câmeras =======================
_store_lock = threading.RLock()

def load_cameras():
    if not CAMERAS_FILE.exists():
        save_cameras(SEED)
        return list(SEED)
    try:
        return json.loads(CAMERAS_FILE.read_text())
    except Exception:
        return []

def save_cameras(cams):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CAMERAS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cams, ensure_ascii=False, indent=2))
    tmp.replace(CAMERAS_FILE)

def update_camera(cid, patch):
    """Aplica um patch parcial e devolve a câmera atualizada."""
    with _store_lock:
        cams = load_cameras()
        for c in cams:
            if c["id"] == cid:
                c.update(patch)
                save_cameras(cams)
                return c
    raise HTTPException(404, f"câmera '{cid}' não encontrada")

def get_camera(cid):
    for c in load_cameras():
        if c["id"] == cid:
            return c
    raise HTTPException(404, f"câmera '{cid}' não encontrada")

def slugify(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()   # "Portão" -> "portao"
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", s.strip().lower()).strip("-")
    return s or "cam"

# ======================= Cliente DVRIP (Sofia / 34567) =======================
_dvr_lock = threading.Lock()

def _xm_hash(pw):
    m = hashlib.md5(pw.encode()).digest(); o = ""
    for i in range(8):
        n = (m[2*i] + m[2*i+1]) % 62
        o += chr(ord('0')+n) if n < 10 else chr(ord('A')+n-10) if n < 36 else chr(ord('a')+n-36)
    return o

def _pkt(sid, seq, mid, obj):
    b = (json.dumps(obj) + "\n\x00").encode()
    return struct.pack('<BBHIIBBHI', 255, 0, 0, sid, seq, 0, 0, mid, len(b)) + b

def _raw_pkt(sid, seq, mid, payload):
    return struct.pack('<BBHIIBBHI', 255, 0, 0, sid, seq, 0, 0, mid, len(payload)) + payload

def _recv(s):
    h = b""
    while len(h) < 20:
        c = s.recv(20 - len(h))
        if not c: raise IOError("conexão fechada")
        h += c
    ln = struct.unpack('<I', h[16:20])[0]; d = b""
    while len(d) < ln:
        c = s.recv(ln - len(d))
        if not c: break
        d += c
    return h, d

class DVRIP:
    def __init__(self, ip, port=34567, user="admin", pw=""):
        self.ip, self.port, self.user, self.pw = ip, port, user, pw
    def __enter__(self):
        self.s = socket.socket(); self.s.settimeout(6); self.s.connect((self.ip, self.port)); self.seq = 0
        self.s.send(_pkt(0, 0, 1000, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                                      "PassWord": _xm_hash(self.pw), "UserName": self.user}))
        h, d = _recv(self.s); self.sid = struct.unpack('<I', h[4:8])[0]
        r = json.loads(d.decode('latin1').strip('\x00\n'))
        if r.get("Ret") != 100:
            raise IOError(f"login DVRIP Ret={r.get('Ret')}")
        return self
    def __exit__(self, *a):
        try: self.s.close()
        except Exception: pass
    def _cmd(self, mid, obj):
        self.seq += 1
        self.s.send(_pkt(self.sid, self.seq, mid, {**obj, "SessionID": "0x%08X" % self.sid}))
        _, d = _recv(self.s)
        return json.loads(d.decode('latin1').strip('\x00\n'))
    def get(self, name):      return self._cmd(1042, {"Name": name})
    def set(self, name, val): return self._cmd(1040, {"Name": name, name: val})
    def sysinfo(self):        return self._cmd(1020, {"Name": "SystemInfo"})
    def ability(self):        return self._cmd(1360, {"Name": "SystemFunction"})
    def ptz(self, command, step=5):
        return self._cmd(1400, {"Name": "OPPTZControl", "OPPTZControl": {"Command": command, "Parameter": {
            "AUX": {"Number": 0, "Status": "On"}, "Channel": 0, "MenuOpts": "Enter",
            "POINT": {"bottom": 0, "left": 0, "right": 0, "top": 0},
            "Pattern": "Start", "Preset": -1, "Step": step, "Tour": 0}}})
    def talk(self, alaw):
        if not alaw:
            return 0
        audio_format = {"EncodeType": "G711_ALAW"}
        claim = self._cmd(1434, {"Name": "OPTalk", "OPTalk": {
            "Action": "Claim", "AudioFormat": audio_format}})
        if claim.get("Ret") not in (100, 515):
            raise IOError(f"OPTalk Claim Ret={claim.get('Ret')}")
        self.seq += 1
        self.s.sendall(_pkt(self.sid, self.seq, 1430, {"Name": "OPTalk", "OPTalk": {
            "Action": "Start", "AudioFormat": audio_format},
            "SessionID": "0x%08X" % self.sid}))

        frame_size = 320  # 40 ms de G.711 A-law mono a 8 kHz
        started = time.monotonic()
        frames = 0
        for offset in range(0, len(alaw), frame_size):
            frame = alaw[offset:offset + frame_size]
            if len(frame) < frame_size:
                frame += bytes([0xD5]) * (frame_size - len(frame))
            payload = struct.pack('>I', 0x1FA) + bytes((14, 2)) + struct.pack('<H', frame_size) + frame
            self.seq += 1
            self.s.sendall(_raw_pkt(self.sid, self.seq, 1432, payload))
            frames += 1
            delay = started + frames * 0.04 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return frames

# ---- disjuntor de login DVRIP ----------------------------------------------
# A câmera bloqueia a conta depois de N logins recusados seguidos, e grava esse
# bloqueio em flash: sobrevive a boot frio, não tem como destravar por fora.
# Foi assim que a conta 'admin' se perdeu — o app fica pedindo o estado da
# lâmpada de 5 em 5s e, com a credencial ruim, cada volta era um login recusado.
# Só recusa de credencial abre o disjuntor (Ret != 100). Timeout de rede não
# alimenta o anti-bruteforce da câmera, então não conta.
DVR_FAIL_MAX = int(os.getenv("DVR_FAIL_MAX", "3"))
DVR_COOLDOWN = int(os.getenv("DVR_COOLDOWN", "300"))
_dvr_breaker = {}                       # chave -> (recusas, liberado_em)
_dvr_breaker_lock = threading.Lock()

def _dvr_key(cam):
    return f"{cam['ip']}:{cam.get('dvrip_port', 34567)}:{cam.get('user', 'admin')}"

def _dvr_blocked_for(key):
    """Segundos restantes de pausa, ou 0 se pode tentar."""
    with _dvr_breaker_lock:
        fails, until = _dvr_breaker.get(key, (0, 0.0))
        if fails >= DVR_FAIL_MAX and time.time() < until:
            return int(until - time.time()) + 1
    return 0

def _dvr_record(key, ok):
    with _dvr_breaker_lock:
        if ok:
            _dvr_breaker.pop(key, None)
            return
        fails = _dvr_breaker.get(key, (0, 0.0))[0] + 1
        _dvr_breaker[key] = (fails, time.time() + DVR_COOLDOWN)
        if fails == DVR_FAIL_MAX:
            print(f"[dvrip] {key}: {fails} logins recusados seguidos — pausando "
                  f"{DVR_COOLDOWN}s para não bloquear a conta na câmera", flush=True)

class _DVRIPGuarded(DVRIP):
    """DVRIP com disjuntor: desiste depois de DVR_FAIL_MAX recusas seguidas."""
    def __init__(self, cam):
        super().__init__(cam["ip"], cam.get("dvrip_port", 34567),
                         cam.get("user", "admin"), cam.get("pass", ""))
        self._key = _dvr_key(cam)

    def __enter__(self):
        wait = _dvr_blocked_for(self._key)
        if wait:
            raise HTTPException(503, f"login DVRIP pausado por mais {wait}s: credencial "
                                     f"recusada {DVR_FAIL_MAX}x seguidas (a câmera bloqueia "
                                     f"a conta em flash se insistirmos)")
        try:
            out = super().__enter__()
        except Exception as e:
            try: self.s.close()
            except Exception: pass
            # IOError == OSError, então pega timeout de rede junto: só conta
            # quando a mensagem é de login recusado de verdade.
            if "login DVRIP Ret=" in str(e):
                _dvr_record(self._key, False)
            raise
        _dvr_record(self._key, True)
        return out

def dvr(cam):
    return _DVRIPGuarded(cam)

# "Auto" NÃO acende a luz branca nesta câmera (dual-light: no escuro ela escolhe
# o IR). O único modo que realmente liga o LED branco é "KeepOpen" — medido pelo
# brilho do snapshot: Close 69, KeepOpen 113, Auto/Manual/Open 84 (= apagada).
# A câmera aceita e devolve qualquer string sem validar, então Ret=100 e a
# releitura do WorkMode não provam nada: só o brilho da cena prova.
LAMP_MODE = {"on": "KeepOpen", "off": "Close", "smart": "Intelligent", "auto": "Auto"}

def audio_to_alaw(data):
    if not data:
        raise HTTPException(400, "áudio vazio")
    if len(data) > 5_000_000:
        raise HTTPException(413, "áudio excede 5 MB")
    p = subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0",
        "-vn", "-ac", "1", "-ar", "8000", "-acodec", "pcm_alaw",
        "-f", "alaw", "pipe:1",
    ], input=data, capture_output=True, timeout=20)
    if p.returncode != 0 or not p.stdout:
        raise HTTPException(422, f"áudio inválido: {p.stderr.decode('latin1')[:160]}")
    if len(p.stdout) > 8_000 * 60:
        raise HTTPException(413, "mensagem de áudio excede 60 segundos")
    return p.stdout

# ======================= ONVIF PTZ =======================
# O PTZ desta câmera responde por ONVIF (o DVRIP OPPTZControl é ignorado).
# Pan é ESPELHADO (ConsumerPtzMirrorDirect): left=+x, right=-x.
PTZ_VEL = {  # (pan_x, tilt_y, zoom)
    "left": (1, 0, 0), "right": (-1, 0, 0), "up": (0, 1, 0), "down": (0, -1, 0),
    "leftup": (1, 1, 0), "rightup": (-1, 1, 0), "leftdown": (1, -1, 0), "rightdown": (-1, -1, 0),
    "zoomin": (0, 0, 1), "zoomout": (0, 0, -1),
}

def _wss(user, pw):
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pw.encode()).digest()).decode()
    n64 = base64.b64encode(nonce).decode()
    return ('<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f'<UsernameToken><Username>{user}</Username>'
            f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{n64}</Nonce>'
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            '</UsernameToken></Security></s:Header>')

def _onvif_ptz(cam, body):
    url = f"http://{cam['ip']}:{cam.get('onvif_port', 8899)}/onvif/ptz_service"
    env = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'{_wss(cam.get("user","admin"), cam.get("pass",""))}<s:Body>{body}</s:Body></s:Envelope>')
    with httpx.Client(timeout=8) as h:
        r = h.post(url, content=env.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        return r.status_code, ("Fault" not in r.text)

def onvif_move(cam, x, y, z):
    prof = cam.get("ptz_profile", "000")
    body = (f'<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl"><ProfileToken>{prof}</ProfileToken>'
            f'<Velocity><PanTilt x="{x}" y="{y}" xmlns="http://www.onvif.org/ver10/schema"/>'
            f'<Zoom x="{z}" xmlns="http://www.onvif.org/ver10/schema"/></Velocity></ContinuousMove>')
    return _onvif_ptz(cam, body)

def onvif_stop(cam):
    prof = cam.get("ptz_profile", "000")
    body = (f'<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl"><ProfileToken>{prof}</ProfileToken>'
            '<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>')
    return _onvif_ptz(cam, body)

def xm_rtsp(ip, user, pw, stream):
    return f"rtsp://{ip}:554/user={user}&password={pw}&channel=1&stream={stream}.sdp"

# ======================= Fonte e resolução =======================
CROP_FILTERS = {   # recortes resolução-independentes (usados na CPU)
    "top": "crop=in_w:in_h/2:0:0", "bottom": "crop=in_w:in_h/2:0:in_h/2",
    "left": "crop=in_w/2:in_h:0:0", "right": "crop=in_w/2:in_h:in_w/2:0",
}

def owner_id(cam):
    """Quem é o dono do stream físico (o pai, quando esta câmera é um recorte)."""
    return cam.get("rec_ref") or cam["id"]

def camera_source(cam, quality="sub"):
    # Todas as câmeras (XiongMai ou RTSP genérica) passam pelo relay do go2rtc: uma
    # conexão só com a câmera, não importa quantos streams derivados existam. As
    # câmeras baratas costumam aceitar só 2 ou 3 sessões RTSP simultâneas.
    return f"{GO2RTC_RTSP}/{owner_id(cam)}_{quality}"

def stream_probe(url):
    """Resolução e codec do vídeo de um stream ou arquivo (via ffprobe)."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                            "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                            "-of", "csv=p=0", "-i", str(url)], capture_output=True, timeout=25)
        m = re.search(r"([a-z0-9_]+),(\d{2,5}),(\d{2,5})", p.stdout.decode("latin1"))
        if m:
            return m.group(1), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return None, None, None

def stream_res(url):
    """Resolução de um stream ou arquivo (via ffprobe)."""
    _, w, h = stream_probe(url)
    return w, h

_res_cache = {}   # (owner, quality) -> (w, h)

def source_res(cam, quality):
    """Resolução da fonte, memorizada em cameras.json — o recorte do cuvid é em pixels."""
    ref = owner_id(cam)
    key = (ref, quality)
    if key in _res_cache:
        return _res_cache[key]
    saved = cam.get(f"res_{quality}")
    if isinstance(saved, list) and len(saved) == 2 and all(saved):
        _res_cache[key] = (saved[0], saved[1])
        return _res_cache[key]
    codec, w, h = stream_probe(camera_source(cam, quality))
    if w and h:
        _res_cache[key] = (w, h)
        patch = {f"res_{quality}": [w, h]}
        if codec:
            patch[f"codec_{quality}"] = codec
            _codec_cache[key] = codec
        try:
            update_camera(ref, patch)     # memoriza no dono do stream
        except HTTPException:
            pass
    return w, h

_codec_cache = {}   # (owner, quality) -> "hevc" | "h264" | ...

# Decodificadores NVDEC por codec da câmera. O que não estiver aqui decodifica na CPU.
CUVID_DECODERS = {"hevc": "hevc_cuvid", "h264": "h264_cuvid"}

def source_codec(cam, quality):
    """Codec do vídeo da fonte. Sem informação, assume H.265 (o padrão das XiongMai)."""
    ref = owner_id(cam)
    key = (ref, quality)
    if key not in _codec_cache:
        # recortes (rec_ref) usam o stream do pai, onde o codec fica memorizado
        owner = cam if ref == cam["id"] else next((c for c in load_cameras() if c["id"] == ref), cam)
        saved = owner.get(f"codec_{quality}")
        if not saved:
            return "hevc"
        _codec_cache[key] = saved
    return _codec_cache[key]

def detect_layout(w, h):
    if not (w and h): return None
    if h > w * 1.05:  return "vertical"     # retrato = dois quadros empilhados
    if w > h * 2.2:   return "horizontal"   # bem largo = dois lado a lado
    return None

# ======================= go2rtc: streams derivados em GPU =======================
def _crop_px(crop, w, h):
    """Recorte para o decodificador: '-crop <topo>x<baixo>x<esq>x<dir>' + tamanho resultante."""
    if not crop or not w or not h:
        return None, (w, h)
    half_h, half_w = (h // 2) & ~1, (w // 2) & ~1
    if crop == "top":    return f"0x{h-half_h}x0x0", (w, half_h)
    if crop == "bottom": return f"{h-half_h}x0x0x0", (w, half_h)
    if crop == "left":   return f"0x0x0x{w-half_w}", (half_w, h)
    if crop == "right":  return f"0x0x{half_w}x0",   (half_w, h)
    return None, (w, h)

def _fit(cw, ch, max_w, max_h):
    """Maior tamanho par que cabe em max_w x max_h mantendo a proporção."""
    if not cw or not ch:
        return max_w, max_h
    scale = min(max_w / cw, max_h / ch, 1.0)
    return max(2, int(cw * scale) & ~1), max(2, int(ch * scale) & ~1)

def _live_cmd(cam, quality, profile, crop=None):
    """Comando ffmpeg (fonte 'exec:' do go2rtc) que entrega H.264/AAC pronto para o player."""
    src = camera_source(cam, quality)
    w, h = source_res(cam, quality)
    crop_arg, (cw, ch) = _crop_px(crop, w, h)
    ow, oh = _fit(cw, ch, *profile[:2])
    bitrate = profile[2]

    # -use_wallclock_as_timestamps dá a áudio e vídeo o MESMO relógio (o da chegada).
    # É o que substitui os antigos hacks de setpts/aresample: a câmera entrega PTS
    # independentes por trilha e o gravador descartava o áudio por "chegar tarde".
    inp = ["-fflags", "+genpts+discardcorrupt", "-use_wallclock_as_timestamps", "1",
           "-rtsp_transport", "tcp", "-i", src]

    decoder = CUVID_DECODERS.get(source_codec(cam, quality))
    if USE_GPU and decoder:
        pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", decoder]
        if crop_arg:
            pre += ["-crop", crop_arg]
        if (ow, oh) != (cw, ch):
            pre += ["-resize", f"{ow}x{oh}"]
        vf = ["-vf", "setsar=1"]
        enc = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-profile:v", "baseline",
               "-rc", "cbr", "-b:v", bitrate, "-bufsize", bitrate,
               "-g", "24", "-bf", "0", "-no-scenecut", "1", "-forced-idr", "1"]
    else:
        pre = []
        filters = ([CROP_FILTERS[crop]] if crop in CROP_FILTERS else []) + [f"scale={ow}:{oh}", "setsar=1"]
        vf = ["-vf", ",".join(filters)]
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
               "-profile:v", "baseline", "-b:v", bitrate, "-g", "24", "-bf", "0"]

    # AAC é o que MSE (fMP4) e HLS tocam direto; para WebRTC o go2rtc converte sozinho.
    aud = ["-c:a", "aac", "-ar", "16000", "-b:a", "32k", "-ac", "1"]

    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + pre + inp +
           ["-map", "0:v:0", "-map", "0:a:0?"] + vf + enc + aud +
           ["-f", "rtsp", "-rtsp_transport", "tcp", "{output}"])
    return "exec:" + " ".join(cmd)

def g2_streams(cam):
    """Streams derivados que esta câmera precisa no go2rtc: nome -> fonte."""
    cid, crop = cam["id"], cam.get("crop")
    out = {
        f"v_{cid}_hd": _live_cmd(cam, "main", PROFILE_HD, crop),
        f"v_{cid}_lo": _live_cmd(cam, "sub",  PROFILE_LO, crop),
    }
    if cam.get("record"):
        # gravação: sensor inteiro (sem recorte) — os filhos compartilham este arquivo
        out[f"v_{cid}_rec"] = _live_cmd(cam, "sub", PROFILE_REC, None)
    return out

def g2_base_streams(cam):
    """Streams físicos (o que sai do sensor). Só as câmeras 'donas' têm os seus."""
    cid = cam["id"]
    # video_source="rtsp" tira o vídeo do DVRIP mesmo numa xiongmai. O DVRIP não
    # libera os slots de sessão quando o host reinicia sem fechar as conexões, e
    # aí recusa todo login (Ret=205) — inclusive de usuário inexistente — até a
    # câmera perder energia. O RTSP não tem esse estado preso.
    # Custo: sem backchannel (áudio bidirecional) no stream base.
    if cam.get("driver") == "xiongmai" and cam.get("video_source") != "rtsp":
        base = (f"dvrip://{cam.get('user','admin')}:{cam.get('pass','')}"
                f"@{cam['ip']}:{cam.get('dvrip_port', 34567)}")
        return {f"{cid}_sub":  [f"{base}?channel=0&subtype=1", f"{base}?backchannel=1"],
                f"{cid}_main": [f"{base}?channel=0&subtype=0"]}
    return {f"{cid}_sub":  [cam.get("rtsp_sub") or cam["rtsp_main"]],
            f"{cid}_main": [cam["rtsp_main"]]}

def g2_desired(cams=None, with_derived=True):
    """Mapa completo de streams do go2rtc, derivado de cameras.json."""
    streams = {}
    for cam in (cams if cams is not None else load_cameras()):
        if not cam.get("rec_ref"):          # recortes reaproveitam o stream do pai
            streams.update(g2_base_streams(cam))
    if with_derived:
        for cam in (cams if cams is not None else load_cameras()):
            streams.update(g2_streams(cam))
    return streams

# O go2rtc recusa fontes 'exec:' vindas da API REST ("insecure producer"), então
# a configuração é escrita no YAML e o serviço recarregado — só quando algo muda.
G2_CONFIG = Path(os.getenv("GO2RTC_CONFIG", "/config/go2rtc.yaml"))
G2_HEADER = {
    "api":  {"listen": "127.0.0.1:1984"},
    "rtsp": {"listen": "127.0.0.1:8556"},
    "webrtc": {"listen": ""},   # WHEP na LAN é servido pelo MediaMTX (8889)
    "log":  {"level": "warn"},
}

def g2_write_config(streams):
    """Grava o go2rtc.yaml. Devolve True se o conteúdo mudou."""
    import yaml
    doc = dict(G2_HEADER); doc["streams"] = streams
    body = ("# GERADO PELA API a partir de data/cameras.json — não edite à mão.\n"
            "# Os streams v_<id>_* transcodificam em GPU (hevc_cuvid -> h264_nvenc).\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=10**6))
    try:
        if G2_CONFIG.exists() and G2_CONFIG.read_text() == body:
            return False
    except Exception:
        pass
    G2_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = G2_CONFIG.with_suffix(".tmp")
    tmp.write_text(body); tmp.replace(G2_CONFIG)
    return True

def g2_restart():
    try:
        with httpx.Client(timeout=10) as h:
            h.post(f"{GO2RTC_API}/api/restart")
    except Exception:
        pass   # o go2rtc encerra a conexão ao reiniciar; a falha aqui é esperada
    for _ in range(40):
        time.sleep(0.5)
        if g2_list_streams() is not None:
            return True
    return False

_g2_lock = threading.Lock()

def g2_sync(force=False):
    """Reconcilia o go2rtc com cameras.json. Reinicia só se a config mudou."""
    with _g2_lock:
        # 1ª passada: os streams físicos precisam existir antes de medirmos as
        # resoluções (o recorte do cuvid é em pixels, não em fração).
        if any(f"res_{q}" not in c for c in load_cameras() if not c.get("rec_ref")
                                   for q in ("sub", "main")):
            if g2_write_config(g2_desired(with_derived=False)):
                g2_restart()
        streams = g2_desired()
        if g2_write_config(streams) or force:
            print(f"[go2rtc] config atualizada ({len(streams)} streams), recarregando", flush=True)
            g2_restart()
        return streams

def g2_list_streams():
    try:
        with httpx.Client(timeout=5) as h:
            r = h.get(f"{GO2RTC_API}/api/streams")
            if r.status_code == 200:
                return set(r.json().keys())
    except Exception:
        pass
    return None

# ======================= MediaMTX: só remux e gravação =======================
def _mtx_paths(cam):
    """Nenhum path transcodifica — o go2rtc já entrega H.264/AAC."""
    cid = cam["id"]
    paths = {
        f"live_{cid}": {"source": f"{GO2RTC_RTSP}/v_{cid}_hd", "rtspTransport": "tcp",
                        "sourceOnDemand": True, "sourceOnDemandCloseAfter": "10s"},
        f"prev_{cid}": {"source": f"{GO2RTC_RTSP}/v_{cid}_lo", "rtspTransport": "tcp",
                        "sourceOnDemand": True, "sourceOnDemandCloseAfter": "10s"},
    }
    if cam.get("record"):
        paths[f"cam_{cid}"] = {
            "source": f"{GO2RTC_RTSP}/v_{cid}_rec", "rtspTransport": "tcp",
            "sourceOnDemand": False,
            "record": True, "recordPath": "/recordings/%path/%Y-%m-%d/%Y-%m-%d_%H-%M-%S-%f",
            "recordFormat": "fmp4", "recordSegmentDuration": "600s", "recordDeleteAfter": "0s",
        }
    return paths

def _mtx_put(name, body):
    with httpx.Client(timeout=8) as h:
        r = h.post(f"{MTX_URL}/v3/config/paths/add/{name}", json=body)
        if r.status_code >= 400:
            r = h.patch(f"{MTX_URL}/v3/config/paths/patch/{name}", json=body)
        return r.status_code

def mtx_ensure_camera(cam):
    for name, body in _mtx_paths(cam).items():
        try:
            print(f"[mtx] {name} -> {_mtx_put(name, body)}", flush=True)
        except Exception as e:
            print(f"[mtx] falha {name}: {e}", flush=True)

def mtx_remove_camera(cid):
    with httpx.Client(timeout=8) as h:
        for name in (f"cam_{cid}", f"live_{cid}", f"prev_{cid}"):
            try: h.delete(f"{MTX_URL}/v3/config/paths/delete/{name}")
            except Exception: pass

def mtx_wait_ready(timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with httpx.Client(timeout=3) as h:
                if h.get(f"{MTX_URL}/v3/config/global/get").status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.5)
    return False

def mtx_list_paths():
    try:
        with httpx.Client(timeout=5) as h:
            r = h.get(f"{MTX_URL}/v3/config/paths/list")
            if r.status_code == 200:
                return {p["name"] for p in r.json().get("items", [])}
    except Exception:
        pass
    return None

def ensure_camera(cam):
    g2_sync()
    mtx_ensure_camera(cam)

def reconcile_loop():
    """Recria o que estiver faltando — auto-cura se um dos serviços reiniciar.

    Reescreve só o que está REALMENTE ausente: um PATCH em path existente derruba a
    sessão, e era isso que congelava o vídeo de tempos em tempos.
    """
    first = True
    while True:
        try:
            if mtx_wait_ready(40 if first else 5):
                want_g2 = set(g2_sync())
                have_g2 = g2_list_streams()
                if have_g2 is not None and not want_g2.issubset(have_g2):
                    g2_restart()          # config certa no disco, processo desatualizado
                have_mtx = mtx_list_paths()
                for cam in load_cameras():
                    if have_mtx is None or not set(_mtx_paths(cam)).issubset(have_mtx):
                        mtx_ensure_camera(cam)
        except Exception as e:
            print(f"[reconcile] erro: {e}", flush=True)
        first = False
        time.sleep(30)

# ======================= Snapshot =======================
_snap = {}  # cid -> (ts, bytes)

def grab_snapshot(cam):
    cid = cam["id"]; now = time.time()
    ts, data = _snap.get(cid, (0, b""))
    if now - ts < 2 and data:
        return data
    # Vem do preview (já recortado, já em H.264): abrir o H.265 cru no meio de um
    # GOP produz quadros cinza/blocados em câmeras multi-sensor.
    # Quando o preview estava parado, o transcode na GPU arranca soltando ~1 s de
    # quadros verde-chapado. O verde puro tem crominância V ~20 (imagem normal ~128),
    # então o signalstats+metadata deixa passar só o primeiro quadro de verdade.
    url = f"rtsp://127.0.0.1:{RTSP_PORT}/prev_{cid}"
    tail = ["-frames:v", "1", "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
                        "-t", "8", "-i", url,
                        "-vf", "signalstats,metadata=select:key=lavfi.signalstats.VAVG:value=60:function=greater"]
                       + tail, capture_output=True, timeout=25)
    if p.returncode != 0 or not p.stdout:     # nenhum quadro passou no filtro: pega o que vier
        p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
                            "-i", url] + tail, capture_output=True, timeout=25)
    if p.returncode != 0 or not p.stdout:
        raise HTTPException(502, f"snapshot falhou: {p.stderr.decode('latin1')[:200]}")
    _snap[cid] = (now, p.stdout)
    return p.stdout

# ======================= Storage / pruner =======================
def _files(root):
    out = []
    if root.exists():
        for f in root.rglob("*.mp4"):
            try:
                st = f.stat(); out.append((f, st.st_size, st.st_mtime))
            except FileNotFoundError:
                pass
    return out

def storage_stats():
    files = _files(REC_DIR)
    total = sum(s for _, s, _ in files)
    per = {}
    for f, s, _ in files:
        try:
            cid = f.relative_to(REC_DIR).parts[0]
            if cid.startswith("cam_"): cid = cid[4:]
        except Exception: cid = "?"
        per[cid] = per.get(cid, 0) + s
    return {"used_bytes": total, "used_gb": round(total/1e9, 2),
            "cap_gb": round(MAX_BYTES/1e9, 2),
            "used_pct": round(100*total/MAX_BYTES, 1) if MAX_BYTES else 0,
            "segments": len(files),
            "per_camera_gb": {k: round(v/1e9, 2) for k, v in per.items()}}

def _pruner_loop():
    trigger, target = MAX_BYTES*TRIGGER_PCT, MAX_BYTES*TARGET_PCT
    while True:
        try:
            files = _files(REC_DIR); total = sum(s for _, s, _ in files)
            if total > trigger:
                files.sort(key=lambda x: x[2])
                for f, s, _ in files:
                    if total <= target: break
                    try: f.unlink(); total -= s
                    except FileNotFoundError: pass
                for d in sorted(REC_DIR.rglob("*"), reverse=True):
                    if d.is_dir() and not any(d.iterdir()):
                        try: d.rmdir()
                        except OSError: pass
                print(f"[pruner] podado -> {total/1e9:.1f} GB", flush=True)
        except Exception as e:
            print(f"[pruner] erro: {e}", flush=True)
        time.sleep(60)

# ======================= Gravações: índice temporal =======================
# O nome do segmento carrega o instante de início: 2026-08-03_21-48-38-039412.mp4
SEG_RE  = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")
MAX_SEG = 900.0     # nenhum segmento passa disto (recordSegmentDuration = 600s)
GAP_TOL = 5.0       # folga para considerar dois segmentos contíguos

def _seg_start(path: Path):
    m = SEG_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                                 "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None

def rec_root(cam):
    return REC_DIR / f"cam_{owner_id(cam)}"

_dur_cache = {}   # str(path) -> (mtime, size, duração)

def seg_duration(path: Path, fallback):
    """Duração medida — só é preciso para o último segmento de cada bloco."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return fallback
    key = str(path)
    hit = _dur_cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, timeout=20)
        d = float(p.stdout.decode().strip())
    except Exception:
        d = fallback
    _dur_cache[key] = (st.st_mtime, st.st_size, d)
    return d

def list_segments(cam, day=None):
    """Segmentos ordenados, com início e fim absolutos (epoch)."""
    root = rec_root(cam)
    if not root.exists():
        return []
    it = (root / day).glob("*.mp4") if day else root.rglob("*.mp4")
    segs = []
    for f in it:
        st = _seg_start(f)
        if st is None:
            continue
        try:
            size = f.stat().st_size
        except FileNotFoundError:
            continue
        segs.append({"path": str(f.relative_to(REC_DIR)), "start": st, "size": size, "_f": f})
    segs.sort(key=lambda s: s["start"])
    for i, s in enumerate(segs):
        nxt = segs[i+1]["start"] if i+1 < len(segs) else None
        # a duração é a distância até o próximo segmento — exata enquanto a gravação
        # é contínua. No fim de um bloco (e no arquivo mais recente) medimos de fato.
        if nxt is not None and nxt - s["start"] <= MAX_SEG + GAP_TOL:
            s["dur"] = round(nxt - s["start"], 3)
        else:
            s["dur"] = round(seg_duration(s["_f"], 600.0), 3)
        s["end"] = round(s["start"] + s["dur"], 3)
    for s in segs:
        s.pop("_f", None)
    return segs

def blocks_from(segs):
    """Junta segmentos contíguos nas faixas contínuas que a timeline pinta."""
    out = []
    for s in segs:
        if out and s["start"] - out[-1]["end"] <= GAP_TOL:
            out[-1]["end"] = s["end"]
        else:
            out.append({"start": s["start"], "end": s["end"]})
    return [{"start": round(b["start"], 3), "end": round(b["end"], 3)} for b in out]

def seg_at(segs, t):
    """Índice do segmento que contém t (ou o próximo disponível) e o deslocamento nele."""
    for i, s in enumerate(segs):
        if s["start"] <= t < s["end"]:
            return i, t - s["start"]
    for i, s in enumerate(segs):
        if s["start"] >= t:
            return i, 0.0
    return None, 0.0

def _rec_target(cam, path):
    target = (REC_DIR / path).resolve()
    base = rec_root(cam).resolve()
    if not str(target).startswith(str(base) + os.sep) or not target.is_file():
        raise HTTPException(404, "gravação não encontrada")
    return target

def _playback_video(cam, height):
    """Filtros e codificador da reprodução (recorte da metade + escala)."""
    filters = [CROP_FILTERS[cam["crop"]]] if cam.get("crop") in CROP_FILTERS else []
    filters.append(f"scale=-2:{height}")
    if LOCAL_NVENC:
        enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-profile:v", "high",
               "-rc", "vbr", "-cq", "28", "-b:v", "0", "-g", "48"]
    else:
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
               "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "48"]
    return ["-vf", ",".join(filters)] + enc

async def ffmpeg_stream(cmd, label, cleanup=None):
    """Roda o ffmpeg e devolve os bytes conforme saem, encerrando ao desconectar.

    Assíncrono de propósito: um subprocesso lido de forma bloqueante dentro do
    middleware HTTP não chega a ser iterado, e ainda prenderia uma thread por
    espectador.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    sent = 0
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            sent += len(chunk)
            yield chunk
        if not sent:      # nada saiu: o motivo está no stderr do ffmpeg
            err = (await proc.stderr.read())[:400].decode("latin1")
            print(f"[ffmpeg] {label} sem saída: {err}", flush=True)
    finally:
        if proc.returncode is None:
            try: proc.kill()
            except Exception: pass
        try: await proc.wait()
        except Exception: pass
        if cleanup:
            cleanup()

# ======================= FastAPI =======================
app = FastAPI(title="BozoBráis", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---- sessão do site: cookie assinado de longa duração ----
# A HTTP Basic de antes não servia para o site: o navegador só guarda a credencial
# na memória da sessão e a perde ao fechar (no celular e no app instalado isso é a
# toda hora), aí a janela de senha voltava. O cookie fica em disco e é renovado em uso.
SESSION_COOKIE = "bb_session"
SESSION_TTL    = int(os.getenv("SESSION_DAYS", "365")) * 86400   # Chrome limita a 400 dias
SESSION_FILE   = DATA_DIR / "session.key"

def _load_session_key() -> bytes:
    try:
        k = SESSION_FILE.read_bytes()
        if len(k) >= 32:
            return k
    except FileNotFoundError:
        pass
    k = secrets.token_bytes(32)
    SESSION_FILE.write_bytes(k)
    os.chmod(SESSION_FILE, 0o600)
    return k

SESSION_KEY = _load_session_key()

def _session_sig(exp: int) -> str:
    # usuário e senha entram na assinatura: trocar a senha no .env derruba todas as sessões
    msg = f"{exp}:{AUTH_USER}:{API_KEY}".encode()
    return base64.urlsafe_b64encode(hmac.new(SESSION_KEY, msg, hashlib.sha256).digest()).decode().rstrip("=")

def _session_exp(request) -> int:
    """Validade (epoch) do cookie de sessão, ou 0 se ausente/inválido/vencido."""
    exp_s, _, sig = (request.cookies.get(SESSION_COOKIE) or "").partition(".")
    if not exp_s.isdigit() or not hmac.compare_digest(sig.encode(), _session_sig(int(exp_s)).encode()):
        return 0
    return int(exp_s) if int(exp_s) > time.time() else 0

def _is_https(request) -> bool:
    # o cloudflared termina o TLS e repassa em HTTP; na LAN é HTTP puro (cookie Secure não grudaria)
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"
            or '"https"' in request.headers.get("cf-visitor", ""))

def _set_session(resp: Response, request) -> None:
    exp = int(time.time()) + SESSION_TTL
    resp.set_cookie(SESSION_COOKIE, f"{exp}.{_session_sig(exp)}", max_age=SESSION_TTL, path="/",
                    httponly=True, samesite="lax", secure=_is_https(request))

def _check_auth(request) -> bool:
    if not API_KEY:
        return True
    if _session_exp(request):
        return True
    if request.headers.get("X-API-Key") == API_KEY:
        return True
    if request.query_params.get("key") == API_KEY:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
            if u == AUTH_USER and p == API_KEY:
                return True
        except Exception:
            pass
    if auth.startswith("Bearer ") and auth[7:].strip() == API_KEY:
        return True
    return False

# liberados sem login: healthcheck, a própria tela de login e o que o manifesto/ícones precisam
PUBLIC_PATHS = {"/api/health", "/login", "/logout", "/manifest.json", "/logo.svg",
                "/icon-64.png", "/icon-180.png", "/icon-192.png", "/icon-512.png"}

class AuthMiddleware:
    """Protege tudo (site + API), menos PUBLIC_PATHS.

    Middleware ASGI puro, não BaseHTTPMiddleware: aquele embrulha a resposta e
    encerra respostas em streaming antes da hora — era o que fazia a reprodução
    de gravações chegar vazia ao player.

    O site entra pelo cookie de sessão (tela /login). App e integrações seguem com
    X-API-Key, ?key=, Bearer ou HTTP Basic. Sem credencial, navegação de página vai
    para /login e o resto leva 401 SEM WWW-Authenticate, para o navegador não abrir
    a janela nativa de senha.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (not API_KEY or scope["type"] != "http"     # o endpoint /ws valida sozinho
                or scope.get("path") in PUBLIC_PATHS):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        exp = _session_exp(request)
        if exp and exp - time.time() < SESSION_TTL / 2:
            # renova em uso: quem abre o site de vez em quando nunca vê o login de novo
            renew = Response()
            _set_session(renew, request)
            cookie = [h for h in renew.raw_headers if h[0] == b"set-cookie"]

            async def send_renewed(msg):
                if msg["type"] == "http.response.start":
                    msg["headers"] = list(msg.get("headers", [])) + cookie
                await send(msg)
            return await self.app(scope, receive, send_renewed)
        if exp or _check_auth(request):
            return await self.app(scope, receive, send)
        if scope["method"] == "GET" and "text/html" in request.headers.get("accept", ""):
            nxt = scope["path"] + (f"?{request.url.query}" if request.url.query else "")
            resp = RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=303)
        else:
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        await resp(scope, receive, send)

app.add_middleware(AuthMiddleware)

# ---- login / logout do site ----
_login_fails: dict = {}   # ip -> [instantes das tentativas erradas]
LOGIN_MAX_FAILS, LOGIN_WINDOW = 10, 900

def _client_ip(request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")

def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"

@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if API_KEY and _check_auth(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return FileResponse(WEB_DIR / "login.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})

@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    nxt = _safe_next(str(form.get("next") or "/"))
    ip, now = _client_ip(request), time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(fails) >= LOGIN_MAX_FAILS:
        return RedirectResponse(f"/login?erro=bloqueado&next={quote(nxt, safe='')}", status_code=303)
    user = str(form.get("user") or "").strip().lower()
    pw = str(form.get("password") or "")
    ok = (hmac.compare_digest(user.encode(), AUTH_USER.lower().encode())
          and hmac.compare_digest(pw.encode(), API_KEY.encode()))
    if not ok:
        fails.append(now); _login_fails[ip] = fails
        print(f"[auth] login errado de {ip} ({len(fails)}/{LOGIN_MAX_FAILS})", flush=True)
        await asyncio.sleep(1)
        return RedirectResponse(f"/login?erro=1&next={quote(nxt, safe='')}", status_code=303)
    _login_fails.pop(ip, None)
    resp = RedirectResponse(nxt, status_code=303)
    _set_session(resp, request)
    return resp

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

def _host(request): return (request.headers.get("host") or "").split(":")[0] or "127.0.0.1"

def _stream_urls(cam, host):
    cid = cam["id"]
    # HLS e MSE são relativos (mesma origem, via proxy) -> funcionam na LAN e no túnel.
    # WebRTC aponta direto no MediaMTX: baixa latência na LAN, ignorado no remoto.
    wrtc = PUBLIC_WEBRTC_BASE or f"http://{host}:{WEBRTC_PORT}"
    return {"webrtc": f"{wrtc}/live_{cid}/whep",
            "mse": f"/ws?src=v_{cid}_hd",
            "mse_preview": f"/ws?src=v_{cid}_lo",
            "hls": f"/hls/live_{cid}/index.m3u8",
            "hls_preview": f"/hls/prev_{cid}/index.m3u8",
            "rtsp_live": f"rtsp://{host}:{RTSP_PORT}/live_{cid}",
            "snapshot": f"/api/cameras/{cid}/snapshot"}

# ---- proxy HLS (streaming de verdade: o buffer inteiro matava o low-latency) ----
@app.get("/hls/{path:path}")
async def hls_proxy(path: str, request: Request):
    url = f"http://127.0.0.1:{HLS_PORT}/{path}"
    if request.url.query:
        q = "&".join(kv for kv in request.url.query.split("&") if not kv.startswith("key="))
        if q:
            url += "?" + q
    # follow_redirects: o MediaMTX responde 302 em /<path>/index.m3u8
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)
    try:
        req = client.build_request("GET", url)
        up = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"hls upstream: {e}")

    async def body():
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose(); await client.aclose()

    headers = {k: v for k, v in up.headers.items()
               if k.lower() in ("content-type", "cache-control", "content-length")}
    return StreamingResponse(body(), status_code=up.status_code, headers=headers,
                             media_type=up.headers.get("content-type", "application/octet-stream"))

# ---- proxy WebSocket para o go2rtc (MSE/fMP4: ~0,5 s através do túnel) ----
@app.websocket("/ws")
async def ws_proxy(ws: WebSocket):
    if API_KEY and not _check_auth(ws):
        await ws.close(code=1008); return
    src = ws.query_params.get("src", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", src):
        await ws.close(code=1008); return
    await ws.accept()
    try:
        async with websockets.connect(f"ws://{GO2RTC_HOST}/api/ws?src={src}",
                                      max_size=None, ping_interval=20) as up:
            async def c2s():
                try:
                    while True:
                        await up.send(await ws.receive_text())
                except Exception:
                    pass          # o cliente fechou: encerra o par de tarefas
            async def s2c():
                try:
                    async for msg in up:
                        if isinstance(msg, bytes): await ws.send_bytes(msg)
                        else:                      await ws.send_text(msg)
                except Exception:
                    pass
            tasks = {asyncio.create_task(c2s()), asyncio.create_task(s2c())}
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:
        print(f"[ws] {src}: {e}", flush=True)
    finally:
        try: await ws.close()
        except Exception: pass

def _pub(cam):
    public = {k: cam.get(k) for k in ("id", "name", "ip", "driver", "ptz", "lamp", "record", "crop")}
    public["talk"] = bool(cam.get("talk", cam.get("driver") == "xiongmai"))
    return public

# ---- gerais ----
@app.get("/api/health")
def health(): return {"status": "ok", "time": time.time()}

@app.get("/api/storage")
def storage(): return storage_stats()

# ---- câmeras ----
@app.get("/api/cameras")
def list_cameras(request: Request):
    host = _host(request)
    return [{**_pub(c), "streams": _stream_urls(c, host)}
            for c in load_cameras() if not c.get("hidden")]

@app.get("/api/cameras/{cid}")
def one_camera(cid: str, request: Request):
    c = get_camera(cid)
    return {**_pub(c), "streams": _stream_urls(c, _host(request))}

@app.patch("/api/cameras/{cid}")
def edit_camera(cid: str, request: Request, patch: dict = Body(...)):
    """Edita a câmera. Hoje só o nome — o id continua sendo a chave dos streams."""
    get_camera(cid)
    allowed = {}
    if "name" in patch:
        name = str(patch["name"] or "").strip()
        if not name:
            raise HTTPException(400, "nome não pode ser vazio")
        if len(name) > 60:
            raise HTTPException(400, "nome muito longo (máx. 60)")
        allowed["name"] = name
    if not allowed:
        raise HTTPException(400, "nada para alterar (campos aceitos: name)")
    cam = update_camera(cid, allowed)
    return {**_pub(cam), "streams": _stream_urls(cam, _host(request))}

@app.post("/api/cameras/probe")
def probe(ip: str = Body(...), user: str = Body("admin"), pw: str = Body(""), dvrip_port: int = Body(34567)):
    """Detecta uma câmera XiongMai: modelo + capacidades + URLs RTSP."""
    try:
        with _dvr_lock, DVRIP(ip, dvrip_port, user, pw) as d:
            info = d.sysinfo().get("SystemInfo", {})
            fn = d.ability().get("SystemFunction", {})
    except Exception as e:
        raise HTTPException(502, f"não consegui falar DVRIP com {ip}: {e}")
    other = fn.get("OtherFunction", {})
    preview = fn.get("PreviewFunction", {})
    main = xm_rtsp(ip, user, pw, 0)
    w, h = stream_res(main)
    layout = detect_layout(w, h)
    return {"driver": "xiongmai", "ip": ip, "user": user, "pass": pw, "dvrip_port": dvrip_port,
            "name": info.get("DeviceModel") or ip,
            "model": info.get("DeviceModel"), "hardware": info.get("HardWare"),
            "firmware": info.get("SoftWareVersion"), "serial": info.get("SerialNo"),
            "ptz": bool(other.get("SupportPTZDirectionControl")),
            "lamp": bool(other.get("SupportCameraWhiteLight")),
            "talk": bool(preview.get("Talk")),
            "record": True, "onvif_port": 8899, "ptz_profile": "000",
            "resolution": f"{w}x{h}" if w else None,
            "splittable": bool(layout), "split_layout": layout,
            "rtsp_main": main, "rtsp_sub": xm_rtsp(ip, user, pw, 1)}

def _check_rtsp_url(url, field):
    url = str(url or "").strip()
    # Só RTSP: o ffprobe também abriria file://, http:// etc. com a permissão do container.
    if not re.match(r"^rtsps?://[^\s/]+", url, re.IGNORECASE):
        raise HTTPException(400, f"{field}: informe uma URL rtsp://")
    return url

@app.post("/api/cameras/probe-rtsp")
def probe_rtsp(rtsp_main: str = Body(...), rtsp_sub: str = Body("")):
    """Testa uma câmera RTSP genérica: codec, resolução e se o substream responde."""
    main = _check_rtsp_url(rtsp_main, "URL principal")
    sub = _check_rtsp_url(rtsp_sub, "URL secundária") if str(rtsp_sub or "").strip() else ""
    codec, w, h = stream_probe(main)
    if not w:
        raise HTTPException(502, "não consegui abrir o vídeo dessa URL (confira endereço, usuário e senha)")
    out = {"driver": "rtsp", "ip": urlparse(main).hostname, "rtsp_main": main,
           "codec": codec, "resolution": f"{w}x{h}",
           "gpu": bool(USE_GPU and codec in CUVID_DECODERS)}
    if sub:
        s_codec, sw, sh = stream_probe(sub)
        if not sw:
            raise HTTPException(502, "a URL secundária não respondeu (deixe em branco para usar só a principal)")
        out.update({"rtsp_sub": sub, "sub_codec": s_codec, "sub_resolution": f"{sw}x{sh}"})
    layout = detect_layout(w, h)
    out.update({"splittable": bool(layout), "split_layout": layout})
    return out

@app.post("/api/cameras")
def add_camera(cam: dict = Body(...)):
    if cam.get("driver") == "rtsp":
        return _add_rtsp_camera(cam)
    if not cam.get("ip"):
        raise HTTPException(400, "ip é obrigatório")
    cams = load_cameras()
    cid = slugify(cam.get("id") or cam.get("name") or cam["ip"])
    ids = {c["id"] for c in cams}
    base, n = cid, 2
    while cid in ids:
        cid = f"{base}-{n}"; n += 1
    user = cam.get("user", "admin"); pw = cam.get("pass", "")
    entry = {
        "id": cid, "name": cam.get("name") or cid, "ip": cam["ip"],
        "driver": cam.get("driver", "xiongmai"), "dvrip_port": int(cam.get("dvrip_port", 34567)),
        "user": user, "pass": pw,
        "rtsp_main": cam.get("rtsp_main") or xm_rtsp(cam["ip"], user, pw, 0),
        "rtsp_sub":  cam.get("rtsp_sub")  or xm_rtsp(cam["ip"], user, pw, 1),
        "ptz": bool(cam.get("ptz", False)), "lamp": bool(cam.get("lamp", False)),
        "talk": bool(cam.get("talk", cam.get("driver", "xiongmai") == "xiongmai")),
        "record": bool(cam.get("record", True)),
        "onvif_port": int(cam["onvif_port"]) if cam.get("onvif_port") else None,
        "ptz_profile": cam.get("ptz_profile"),
    }
    with _store_lock:
        cams.append(entry); save_cameras(cams)
    ensure_camera(entry)
    return _pub(entry)

def _new_id(cams, wanted):
    cid = slugify(wanted)
    ids = {c["id"] for c in cams}
    base, n = cid, 2
    while cid in ids:
        cid = f"{base}-{n}"; n += 1
    return cid

def _add_rtsp_camera(cam):
    """Câmera genérica: vídeo por RTSP e, opcionalmente, PTZ por ONVIF."""
    main = _check_rtsp_url(cam.get("rtsp_main"), "URL principal")
    sub = _check_rtsp_url(cam.get("rtsp_sub"), "URL secundária") if str(cam.get("rtsp_sub") or "").strip() else ""
    ip = urlparse(main).hostname
    onvif_port = int(cam["onvif_port"]) if str(cam.get("onvif_port") or "").strip() else None
    cams = load_cameras()
    entry = {
        "id": _new_id(cams, cam.get("name") or ip), "name": cam.get("name") or ip, "ip": ip,
        "driver": "rtsp", "rtsp_main": main, "rtsp_sub": sub or None,
        # usuário/senha aqui servem só ao ONVIF (o RTSP leva os dele na própria URL)
        "user": cam.get("user") or "admin", "pass": cam.get("pass") or "",
        "ptz": bool(onvif_port and cam.get("ptz")), "lamp": False, "talk": False,
        "record": bool(cam.get("record", True)),
        "onvif_port": onvif_port,
        "ptz_profile": (cam.get("ptz_profile") or "").strip() or "Profile_1",
    }
    with _store_lock:
        cams.append(entry); save_cameras(cams)
    ensure_camera(entry)
    return _pub(entry)

@app.delete("/api/cameras/{cid}")
def del_camera(cid: str):
    with _store_lock:
        cams = load_cameras()
        if not any(c["id"] == cid for c in cams):
            raise HTTPException(404, "câmera não encontrada")
        save_cameras([c for c in cams if c["id"] != cid])
    mtx_remove_camera(cid); g2_sync()
    return {"deleted": cid}

# ---- funções por câmera ----
@app.get("/api/cameras/{cid}/snapshot")
def snapshot(cid: str):
    return Response(content=grab_snapshot(get_camera(cid)), media_type="image/jpeg")

def _ptz_vec(cam, dir, speed):
    if not cam.get("ptz"):
        raise HTTPException(409, "câmera sem PTZ")
    v = PTZ_VEL.get(dir.lower())
    if not v:
        raise HTTPException(400, f"direção inválida: {', '.join(PTZ_VEL)}")
    speed = max(0.1, min(float(speed), 1.0))
    return tuple(round(c * speed, 3) for c in v)

@app.api_route("/api/cameras/{cid}/ptz", methods=["GET", "POST"])
def ptz(cid: str, dir: str = Query(...), speed: float = Query(0.7),
        duration: float = Query(0.5, ge=0.1, le=3.0)):
    """Nudge: move na direção por `duration` s e para (bom para toques)."""
    cam = get_camera(cid)
    x, y, z = _ptz_vec(cam, dir, speed)
    code, ok = onvif_move(cam, x, y, z)
    time.sleep(duration)
    onvif_stop(cam)
    return {"dir": dir, "http": code, "ok": ok}

@app.post("/api/cameras/{cid}/ptz/start")
def ptz_start(cid: str, dir: str = Query(...), speed: float = Query(0.7)):
    """Início de movimento contínuo (pressionar). Chame /ptz/stop ao soltar."""
    cam = get_camera(cid)
    x, y, z = _ptz_vec(cam, dir, speed)
    code, ok = onvif_move(cam, x, y, z)
    return {"dir": dir, "http": code, "ok": ok}

@app.post("/api/cameras/{cid}/ptz/stop")
def ptz_stop(cid: str):
    cam = get_camera(cid)
    if not cam.get("ptz"):
        raise HTTPException(409, "câmera sem PTZ")
    code, ok = onvif_stop(cam)
    return {"http": code, "ok": ok}

@app.get("/api/cameras/{cid}/lamp")
def lamp_get(cid: str):
    cam = get_camera(cid)
    if not cam.get("lamp"):
        raise HTTPException(409, "câmera sem lâmpada")
    with _dvr_lock, dvr(cam) as d:
        wl = d.get("Camera.WhiteLight").get("Camera.WhiteLight", {})
    inv = {"KeepOpen": "on", "Close": "off", "Intelligent": "smart", "Auto": "auto"}
    return {"workmode": wl.get("WorkMode"), "state": inv.get(wl.get("WorkMode"), wl.get("WorkMode")),
            "brightness": wl.get("Brightness")}

@app.post("/api/cameras/{cid}/lamp")
def lamp_set(cid: str, state: str = Query(...)):
    cam = get_camera(cid)
    if not cam.get("lamp"):
        raise HTTPException(409, "câmera sem lâmpada")
    mode = LAMP_MODE.get(state.lower())
    if not mode:
        raise HTTPException(400, "state deve ser on, off ou smart")
    with _dvr_lock, dvr(cam) as d:
        wl = d.get("Camera.WhiteLight").get("Camera.WhiteLight", {})
        wl["WorkMode"] = mode
        if state.lower() == "on":
            wl["Brightness"] = 100
            # NÃO reescrever WorkPeriod aqui: é a janela horária guardada na
            # câmera, e sobrescrevê-la a cada clique em "ligar" apagava o
            # agendamento do usuário.
        r = d.set("Camera.WhiteLight", wl)
    return {"state": state.lower(), "workmode": mode, "ret": r.get("Ret")}

def _hhmm(s):
    """'18:00' -> minutos desde a meia-noite. None se vazio ou inválido."""
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    return None

def _lamp_write(cam, on):
    with _dvr_lock, dvr(cam) as d:
        wl = d.get("Camera.WhiteLight").get("Camera.WhiteLight", {})
        wl["WorkMode"] = LAMP_MODE["on" if on else "off"]
        if on:
            wl["Brightness"] = 100
        return d.set("Camera.WhiteLight", wl)

@app.get("/api/cameras/{cid}/lamp/schedule")
def lamp_schedule_get(cid: str):
    cam = get_camera(cid)
    if not cam.get("lamp"):
        raise HTTPException(409, "câmera sem lâmpada")
    sch = (get_camera(owner_id(cam)).get("lamp_schedule") or
           {"enable": False, "on": "18:00", "off": "05:00"})
    return {**sch, "applies_to": owner_id(cam)}

@app.post("/api/cameras/{cid}/lamp/schedule")
def lamp_schedule_set(cid: str, enable: bool = Body(True), on: str = Body("18:00"),
                      off: str = Body("05:00")):
    cam = get_camera(cid)
    if not cam.get("lamp"):
        raise HTTPException(409, "câmera sem lâmpada")
    if _hhmm(on) is None or _hhmm(off) is None:
        raise HTTPException(400, "horários devem ser HH:MM")
    sch = {"enable": bool(enable), "on": on.strip(), "off": off.strip()}
    # Os recortes dividem o mesmo LED físico: a agenda mora sempre no pai.
    update_camera(owner_id(cam), {"lamp_schedule": sch})
    return {**sch, "applies_to": owner_id(cam)}

def lamp_sched_loop():
    """Só dispara na virada do horário (borda), nunca a cada volta do laço.
    É isso que deixa ligar/desligar pelo app continuar valendo: entre um evento
    e o próximo o agendador não encosta na luz."""
    last = None   # minuto-do-dia da última verificação
    while True:
        try:
            now = datetime.now()
            cur = now.hour * 60 + now.minute
            gap = None if last is None else (cur - last) % 1440
            # last=None (recém-subiu) ou buraco grande (máquina suspensa): só
            # ressincroniza, sem disparar nada — senão atropelaria o usuário.
            if gap is not None and 0 < gap <= 5:
                cruzados = [(last + i) % 1440 for i in range(1, gap + 1)]
                for cam in load_cameras():
                    if not cam.get("lamp") or cam.get("rec_ref"):
                        continue
                    sch = cam.get("lamp_schedule") or {}
                    if not sch.get("enable"):
                        continue
                    on_m, off_m = _hhmm(sch.get("on")), _hhmm(sch.get("off"))
                    for m in cruzados:
                        if m not in (on_m, off_m):
                            continue
                        acender = (m == on_m)
                        try:
                            r = _lamp_write(cam, acender)
                            print(f"[lâmpada] {cam['id']}: agenda "
                                  f"{'ligou' if acender else 'desligou'} "
                                  f"({sch.get('on')}/{sch.get('off')}) Ret={r.get('Ret')}", flush=True)
                        except Exception as e:
                            print(f"[lâmpada] {cam['id']}: agenda falhou: {e}", flush=True)
            last = cur
        except Exception as e:
            print(f"[lâmpada] laço da agenda: {e}", flush=True)
        time.sleep(20)

@app.post("/api/cameras/{cid}/talk")
async def talk(cid: str, request: Request):
    """Converte uma mensagem de voz e reproduz no alto-falante da câmera.

    Aceita o áudio como corpo bruto (Content-Type: audio/*, o formato exato não
    importa — o ffmpeg detecta) ou como multipart/form-data (campo "audio"),
    que é como a maioria dos clientes HTTP mobile envia arquivo gravado.
    """
    cam = get_camera(cid)
    if not cam.get("talk", cam.get("driver") == "xiongmai"):
        raise HTTPException(409, "câmera sem áudio bidirecional")
    if cam.get("driver") != "xiongmai":
        raise HTTPException(409, "contrafluxo de áudio indisponível para este modelo")
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("audio")
        if upload is None:
            raise HTTPException(400, "multipart sem o campo 'audio'")
        audio = await upload.read()
    else:
        audio = await request.body()
    alaw = audio_to_alaw(audio)
    try:
        with _dvr_lock, dvr(cam) as client:
            frames = client.talk(alaw)
    except Exception as e:
        raise HTTPException(502, f"falha ao enviar áudio para a câmera: {e}")
    return {"ok": True, "duration_ms": frames * 40}

# ---- gravações: lista por arquivo (contrato antigo, usado pelo app mobile) ----
@app.get("/api/cameras/{cid}/recordings")
def recordings(cid: str, limit: int = Query(200, ge=1, le=2000)):
    cam = get_camera(cid)
    segs = list_segments(cam)
    segs.sort(key=lambda s: s["start"], reverse=True)
    items = [{"path": s["path"], "size_mb": round(s["size"]/1e6, 1),
              "mtime": int(s["start"]), "start": s["start"], "duration": s["dur"],
              "url": f"/api/cameras/{cid}/recordings/file?path={s['path']}"}
             for s in segs[:limit]]
    return {"count": len(segs), "items": items, "shared": bool(cam.get("rec_ref"))}

@app.get("/api/cameras/{cid}/recordings/file")
def recording_file(cid: str, path: str):
    return FileResponse(_rec_target(get_camera(cid), path), media_type="video/mp4")

@app.get("/api/cameras/{cid}/recordings/play")
def recording_play(cid: str, path: str, at: float = Query(0, ge=0), height: int = Query(720, ge=180, le=1440)):
    """Reproduz um arquivo específico (contrato antigo). Recorta a metade da câmera."""
    cam = get_camera(cid)
    target = _rec_target(cam, path)
    cmd = (["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{at:.3f}",
            "-fflags", "+genpts+discardcorrupt", "-i", str(target), "-map", "0:v:0", "-an"]
           + _playback_video(cam, height)
           + ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
    return StreamingResponse(ffmpeg_stream(cmd, f"play {cid}"), media_type="video/mp4")

@app.get("/api/cameras/{cid}/recordings/thumbnail")
def recording_thumbnail(cid: str, path: str, at: float = Query(0, ge=0)):
    """Extrai um quadro real da gravação (contrato antigo)."""
    cam = get_camera(cid)
    return _frame_response(cam, _rec_target(cam, path), at)

# ---- gravações: linha do tempo ----
@app.get("/api/cameras/{cid}/timeline/days")
def timeline_days(cid: str):
    """Dias que têm gravação, do mais novo para o mais antigo."""
    root = rec_root(get_camera(cid))
    days = sorted((d.name for d in root.iterdir()
                   if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)),
                  reverse=True) if root.exists() else []
    return {"days": days}

@app.get("/api/cameras/{cid}/timeline")
def timeline(cid: str, date: str = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Faixas gravadas do dia — é o que a régua desenha."""
    cam = get_camera(cid)
    segs = list_segments(cam, date)
    if not segs:
        return {"date": date, "blocks": [], "first": None, "last": None, "segments": 0}
    return {"date": date, "blocks": blocks_from(segs),
            "first": segs[0]["start"], "last": segs[-1]["end"], "segments": len(segs)}

@app.get("/api/cameras/{cid}/playback")
def playback(cid: str, t: float = Query(...), height: int = Query(720, ge=180, le=1440),
             span: float = Query(7200, ge=10, le=86400)):
    """Reprodução contínua a partir do instante `t` (epoch), atravessando arquivos.

    Emenda os segmentos seguintes num único MP4 fragmentado, então o player pode
    correr por horas sem trocar de fonte — é o que faz a régua se comportar como
    uma timeline de editor em vez de uma lista de arquivos.
    """
    cam = get_camera(cid)
    day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    segs = list_segments(cam, day)
    # o instante pode cair perto da virada do dia: junta o dia seguinte
    nxt = datetime.fromtimestamp(t + span).strftime("%Y-%m-%d")
    if nxt != day:
        segs += list_segments(cam, nxt)
    segs.sort(key=lambda s: s["start"])
    idx, offset = seg_at(segs, t)
    if idx is None:
        raise HTTPException(404, "não há gravação a partir deste instante")

    # só emenda enquanto for contínuo: um buraco vira o fim desta reprodução
    chosen, limit = [], segs[idx]["start"] + offset + span
    for s in segs[idx:]:
        if chosen and s["start"] - chosen[-1]["end"] > GAP_TOL:
            break
        if s["start"] > limit:
            break
        chosen.append(s)

    lines = ["ffconcat version 1.0"]
    for i, s in enumerate(chosen):
        lines.append(f"file '{(REC_DIR / s['path']).as_posix()}'")
        if i == 0 and offset > 0.05:
            lines.append(f"inpoint {offset:.3f}")
    # a playlist vai em arquivo, não em pipe: -nostdin (que precisamos, senão o
    # ffmpeg engole o terminal) fecha o stdin e o demuxer concat não leria nada.
    pl = Path(f"/tmp/pb_{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000)}.txt")
    pl.write_text("\n".join(lines) + "\n")

    # readrate: sem isto o ffmpeg despeja as próximas horas de vídeo o mais rápido
    # que a GPU aguenta. O burst inicial enche o buffer do player na hora; depois
    # segue a 3x o tempo real, que é folga de sobra para avançar sem engasgar.
    cmd = (["ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
            "-readrate", "3", "-readrate_initial_burst", "15",
            "-f", "concat", "-safe", "0", "-i", str(pl), "-map", "0:v:0", "-an"]
           + _playback_video(cam, height)
           + ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
    gen = ffmpeg_stream(cmd, f"playback {cid} t={t:.0f}",
                        cleanup=lambda: pl.unlink(missing_ok=True))
    return StreamingResponse(gen, media_type="video/mp4", headers={
        "X-Playback-Start": f"{segs[idx]['start'] + offset:.3f}",
        "X-Playback-End": f"{chosen[-1]['end']:.3f}",
        "Cache-Control": "no-store"})

def _frame_response(cam, target, at):
    filters = [CROP_FILTERS[cam["crop"]]] if cam.get("crop") in CROP_FILTERS else []
    filters.append("scale=320:-2")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{at:.3f}",
           "-fflags", "+genpts+discardcorrupt", "-i", str(target),
           "-map", "0:v:0", "-an", "-vf", ",".join(filters),
           "-frames:v", "1", "-q:v", "4", "-f", "image2", "pipe:1"]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "tempo esgotado ao gerar miniatura")
    if r.returncode or not r.stdout:
        raise HTTPException(422, "não foi possível extrair este quadro")
    return Response(content=r.stdout, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.get("/api/cameras/{cid}/frame")
def frame_at(cid: str, t: float = Query(...)):
    """Quadro do instante `t` (epoch) — a prévia que segue o marcador da régua."""
    cam = get_camera(cid)
    day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    segs = list_segments(cam, day)
    idx, offset = seg_at(segs, t)
    if idx is None:
        raise HTTPException(404, "sem gravação neste instante")
    return _frame_response(cam, REC_DIR / segs[idx]["path"], offset)

@app.post("/api/cameras/{cid}/split")
def split_camera(cid: str, layout: str = Query("auto"), ptz_child: int = Query(-1)):
    """Divide uma câmera multi-sensor empilhada em duas (recorte topo/baixo ou esq/dir).
    O pai vira gravador oculto (grava o combinado uma vez); os filhos mostram cada metade."""
    parent = get_camera(cid)
    if parent.get("crop"):
        raise HTTPException(400, "esta câmera já é uma metade (não dá pra dividir de novo)")
    if layout == "auto":
        w, h = source_res(parent, "main")
        layout = detect_layout(w, h) or ""
    crops = {"vertical": ["top", "bottom"], "horizontal": ["left", "right"]}.get(layout)
    if not crops:
        raise HTTPException(400, "câmera não parece multi-sensor empilhada (não splittable)")
    labels = {"top": "Superior", "bottom": "Inferior", "left": "Esquerda", "right": "Direita"}
    ptz_idx = ptz_child if ptz_child >= 0 else (len(crops) - 1 if parent.get("ptz") else -1)
    with _store_lock:
        cams = load_cameras()
        for c in cams:
            if c["id"] == cid:
                c["hidden"] = True; c["record"] = True
        ids = {c["id"] for c in cams}
        children = []
        for i, crop in enumerate(crops):
            chid, n = f"{cid}-{crop}", 2
            while chid in ids:
                chid = f"{cid}-{crop}-{n}"; n += 1
            ids.add(chid)
            child = {**{k: parent.get(k) for k in ("ip", "driver", "dvrip_port", "user", "pass",
                        "rtsp_main", "rtsp_sub", "onvif_port", "ptz_profile", "lamp", "talk")},
                     "id": chid, "name": f'{parent["name"]} — {labels.get(crop, crop)}',
                     "crop": crop, "record": False, "rec_ref": cid,
                     "ptz": bool(parent.get("ptz")) and i == ptz_idx}
            children.append(child)
        cams.extend(children); save_cameras(cams)
    ensure_camera(get_camera(cid))
    for ch in children:
        ensure_camera(ch)
    return {"parent": cid, "layout": layout, "children": [_pub(c) for c in children]}

# ---- estáticos (por último) ----
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

@app.on_event("startup")
def _startup():
    global LOCAL_NVENC
    LOCAL_NVENC = _local_nvenc()
    threading.Thread(target=lambda: (g2_sync(), None), daemon=True).start()
    threading.Thread(target=_pruner_loop, daemon=True).start()
    threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=lamp_sched_loop, daemon=True).start()
    print(f"[api] gpu-live={'on' if USE_GPU else 'off'} gpu-playback={'on' if LOCAL_NVENC else 'off'} "
          f"cap={MAX_BYTES/1e9:.0f}GB trigger={TRIGGER_PCT*100:.0f}% "
          f"cameras={len(load_cameras())}", flush=True)
