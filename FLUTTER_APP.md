# App Mobile (Flutter/Android) — Câmeras

Guia completo para construir o app: **base URL, autenticação, todos os endpoints, streaming, modelos de dados e a especificação de UX** (navigation drawer + galeria + PTZ + áudio).

---

## 1. Conexão e base URL

| Ambiente | Base URL |
|---|---|
| **Remoto (cloudflared)** | `https://cameras.seudominio.com` |
| LAN (opcional) | `http://192.168.1.10:8091` |

Tudo — API, imagens, gravações e **vídeo ao vivo (HLS)** — sai da **mesma origem** (o túnel aponta para um único serviço). Use sempre a base acima; nunca portas separadas no remoto.

> **WebRTC não passa pelo Cloudflare** (usa UDP). No mobile/remoto use **HLS** (abaixo). O app não precisa de WebRTC.

## 2. Autenticação

Senha única protege tudo. Envie em **todas** as requisições (inclusive no player de vídeo):

```
Header:  X-API-Key: <API_KEY>
```
(Alternativa equivalente: HTTP Basic `<AUTH_USER>:<API_KEY>`.)

- Sem credencial → **401**. Guarde a senha no `flutter_secure_storage`.
- Tela de login: peça a senha (e opcionalmente a base URL), teste com `GET /api/health` + `GET /api/cameras`, e salve.
- **O player HLS precisa mandar o header em toda requisição** (manifesto + segmentos) — use `httpHeaders` (veja §6).

---

## 3. Endpoints

Base = `https://cameras.seudominio.com`. Todos sob `/api` exigem o header, exceto `/api/health`.

### Câmeras
| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/health` | Status (sem auth). `{"status":"ok"}` |
| `GET` | `/api/cameras` | **Lista de câmeras** com flags e URLs de stream (monta a UI a partir daqui) |
| `GET` | `/api/cameras/{id}` | Uma câmera |
| `POST` | `/api/cameras/probe` | Detecta uma câmera nova. Body JSON `{"ip","user","pw"}` → modelo, PTZ, luz, resolução, `splittable` |
| `POST` | `/api/cameras` | Adiciona (envie o objeto do probe, ou `{ip,user,pass,name}`) |
| `DELETE` | `/api/cameras/{id}` | Remove |
| `POST` | `/api/cameras/{id}/split?layout=auto` | Divide câmera multi-sensor em duas metades |

### Vídeo, foto, controle
| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/cameras/{id}/snapshot` | JPEG atual (bom p/ miniatura da galeria) |
| `POST` | `/api/cameras/{id}/ptz/start?dir=left&speed=0.7` | Início do movimento (ao **pressionar**) |
| `POST` | `/api/cameras/{id}/ptz/stop` | Para (ao **soltar**) |
| `POST` | `/api/cameras/{id}/ptz?dir=left&speed=0.7&duration=0.5` | Nudge (um toque = move `duration`s e para) |
| `GET` | `/api/cameras/{id}/lamp` | Estado da luz `{state:"on|off|smart"}` |
| `POST` | `/api/cameras/{id}/lamp?state=on` | Liga/desliga a luz branca (`on`/`off`/`smart`) |
| `GET/POST` | `/api/cameras/{id}/lamp/schedule` | Agenda da luz. Body `{"enable":true,"on":"18:00","off":"05:00"}` |
| `POST` | `/api/cameras/{id}/talk` | Toca uma mensagem de voz no alto-falante da câmera (áudio bidirecional). Só câmeras com `"talk": true` (ver modelo de dados). Corpo: **o arquivo de áudio bruto**, `Content-Type` do gravador (qualquer formato — o servidor detecta com ffmpeg), **ou** `multipart/form-data` com o campo `audio`. Máx. 5 MB / 60 s. Resposta `{"ok":true,"duration_ms":1000}`. `409` = câmera sem esse recurso. |

`dir` ∈ `up,down,left,right,leftup,rightup,leftdown,rightdown,zoomin,zoomout`. `speed` 0.1–1.0.

```dart
// multipart, forma mais comum a partir de um gravador mobile (arquivo .m4a/.wav/etc — tanto faz)
var req = http.MultipartRequest('POST', Uri.parse('$base/api/cameras/$id/talk'))
  ..headers.addAll(headers)
  ..files.add(await http.MultipartFile.fromPath('audio', recordedFilePath));
await req.send();
```

### Gravações — linha do tempo (preferir estas)
| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/cameras/{id}/timeline/days` | `{days:["2026-08-04", …]}` (mais novo primeiro) |
| `GET` | `/api/cameras/{id}/timeline?date=AAAA-MM-DD` | `{blocks:[{start,end}], first, last, segments}` — `start`/`end` em **epoch (segundos)**. É o que a régua pinta. |
| `GET` | `/api/cameras/{id}/playback?t=<epoch>&height=720` | **Reprodução contínua** a partir do instante `t`, emendando os arquivos seguintes. MP4 fragmentado; toca direto no `video_player`. |
| `GET` | `/api/cameras/{id}/frame?t=<epoch>` | JPEG do instante (prévia enquanto arrasta o marcador) |

`playback` também aceita `span=<segundos>` (padrão 7200) para limitar quanto emenda,
e devolve os cabeçalhos `X-Playback-Start` / `X-Playback-End` com o intervalo real.
Para **navegar**, refaça a requisição com outro `t` — não há seek dentro do stream.

### Gravações — por arquivo (contrato antigo, continua valendo)
| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/cameras/{id}/recordings?limit=300` | Lista `{count, items:[{path,size_mb,mtime,start,duration,url}], shared}` |
| `GET` | `/api/cameras/{id}/recordings/play?path=...` | Reprodução de um arquivo (H264, altura configurável em `height`) |
| `GET` | `/api/cameras/{id}/recordings/file?path=...` | Download do arquivo original (suporta `Range`/seek) |

### Storage
| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/storage` | `{used_gb, cap_gb, used_pct, segments, per_camera_gb}` |

---

## 4. Modelos de dados

### Câmera (item de `/api/cameras`)
```json
{
  "id": "cam1-bottom",
  "name": "Câmera 1 — Inferior",
  "ip": "192.168.1.50",
  "driver": "xiongmai",
  "ptz": true,          // mostrar controle PTZ?
  "lamp": true,         // mostrar botão de luz?
  "talk": true,         // mostrar botão de falar/enviar áudio? (POST .../talk)
  "record": false,      // grava direto? (metades compartilham a gravação do pai)
  "crop": "bottom",     // null | top | bottom | left | right
  "streams": {
    "webrtc": "http://192.168.1.10:8889/live_cam1-bottom/whep",  // só LAN; ignore no mobile
    "hls":         "/hls/live_cam1-bottom/index.m3u8",  // 720p — tela da câmera
    "hls_preview": "/hls/prev_cam1-bottom/index.m3u8",  // 360p — miniatura da galeria
    "mse":         "/ws?src=v_cam1-bottom_hd",          // WebSocket (fMP4); web usa, mobile não precisa
    "mse_preview": "/ws?src=v_cam1-bottom_lo",
    "rtsp_live": "rtsp://192.168.1.10:8554/live_cam1-bottom",
    "snapshot": "/api/cameras/cam1-bottom/snapshot"     // RELATIVO -> prefixe a base URL
  }
}
```
> **Sempre** prefixe `hls`, `hls_preview` e `snapshot` com a base URL.
> Ex.: `https://cameras.seudominio.com` + `/hls/live_cam1-bottom/index.m3u8`.

Na **galeria** use `hls_preview` (360p): é o perfil leve, feito para várias câmeras
tocando ao mesmo tempo. Na **tela da câmera** use `hls` (720p, vem do sensor principal).

Toda câmera tem **áudio** (AAC no HLS) — o toggle de áudio aparece sempre.

---

## 5. Especificação de UX

### Estrutura (Material 3, tema escuro, visual moderno)
- **Navigation Drawer** (menu lateral): itens **Galeria**, **Gravações**, **Adicionar câmera**, **Ajustes** (base URL / sair). Cabeçalho com título e status de armazenamento (`/api/storage`).
- **Tela inicial = Galeria**: grid responsivo (`GridView`, 2 colunas retrato / 3–4 paisagem) com um card por câmera:
  - vídeo ao vivo (HLS) **ou** miniatura via `snapshot` que atualiza a cada ~5s (mais leve p/ muitas câmeras);
  - nome embaixo; ícone **⤢** no canto para abrir em tela cheia.
- **Ao tocar num card → tela da câmera** (single). Nessa tela:
  - **Trocar de câmera deslizando**: use `PageView` com as câmeras; mostre **setas/ícone de clique para o lado** (‹ ›) nas bordas para alternar sem swipe.
  - **Áudio**: ícone no **canto superior** (ex.: direito). **Ativo por padrão** (som ligado ao abrir a câmera). Toca → muta/desmuta. (Na galeria, mantenha mudo para não sobrepor sons.)
  - **PTZ** (só quando `ptz == true`): ícone no **canto inferior direito**; ao tocar, abre um **D-pad** flutuante (↖↑↗ ← → ↙↓↘ + zoom). Botões são **pressionar-e-segurar**: `onTapDown → /ptz/start`, `onTapUp/Cancel → /ptz/stop`.
  - **Zoom**: pinça (`InteractiveViewer`) para zoom digital na imagem.
  - Rodapé com: foto (`snapshot`), **luz** (só se `lamp==true`, destaca quando ligada), gravações.

### Regras condicionais ("sempre que existirem")
- `ptz==false` → **não** mostra o controle PTZ.
- `lamp==false` → **não** mostra o botão de luz.
- `record==false` (metades) → gravações vêm da câmera-pai (`shared:true`); ainda funcionam normalmente.

### Tela de Gravações (linha do tempo, não lista de arquivos)
- Seletor de **câmera** + **data** (`/timeline/days` preenche as datas válidas).
- **Régua horizontal** com as faixas gravadas de `/timeline?date=…` (`blocks` em epoch),
  marcador arrastável, pinça para zoom (de 1 min a 24 h de janela).
- Ao soltar o marcador em `t`: `video_player` aponta para `/playback?t=<t>&height=720`.
  Enquanto arrasta, mostre a prévia de `/frame?t=<t>`.
- O tempo absoluto do player é `X-Playback-Start + controller.value.position`.
- Botão de download do trecho: `/playback?t=<t>&span=600`.

---

## 6. Streaming no Flutter (HLS + header de auth)

Use **`video_player`** (ou `better_player`) — ambos aceitam headers em TODAS as requisições HLS.

```dart
final base = 'https://cameras.seudominio.com';
final headers = {'X-API-Key': apiKey};

// AO VIVO (HLS):
final liveUrl = base + cam.streams.hls;      // ex.: .../hls/live_cam1-bottom/index.m3u8
final controller = VideoPlayerController.networkUrl(
  Uri.parse(liveUrl),
  httpHeaders: headers,                       // vai no manifesto E nos segmentos
  formatHint: VideoFormat.hls,
)..initialize().then((_) {
  controller.setVolume(1.0);                  // áudio ATIVO por padrão
  controller.play();
});

// SNAPSHOT (miniatura da galeria):
Image.network('$base${cam.streams.snapshot}?t=$ts', headers: headers);

// GRAVAÇÃO:
VideoPlayerController.networkUrl(
  Uri.parse('$base/api/cameras/${cam.id}/recordings/play?path=${Uri.encodeComponent(path)}'),
  httpHeaders: headers, formatHint: VideoFormat.other);
```

### PTZ (pressionar e segurar)
```dart
Future<void> ptzStart(String id, String dir) =>
  http.post(Uri.parse('$base/api/cameras/$id/ptz/start?dir=$dir&speed=0.7'), headers: headers);
Future<void> ptzStop(String id) =>
  http.post(Uri.parse('$base/api/cameras/$id/ptz/stop'), headers: headers);

GestureDetector(
  onTapDown: (_) => ptzStart(cam.id, 'left'),
  onTapUp:   (_) => ptzStop(cam.id),
  onTapCancel: () => ptzStop(cam.id),
  child: Icon(Icons.chevron_left),
);
```

### Luz
```dart
http.post(Uri.parse('$base/api/cameras/$id/lamp?state=on'), headers: headers);   // on|off|smart
```

### Pacotes sugeridos
`dio` ou `http` · `video_player` (ou `better_player`) · `flutter_secure_storage` · `provider`/`riverpod` · Material 3.

> **Latência:** HLS remoto tem ~2–5 s de atraso (normal p/ Cloudflare). Na LAN dá pra usar WebRTC (`streams.webrtc`) via `flutter_webrtc` para <1 s, mas é opcional.

---

## 7. Observações
- **Câmeras multi-sensor** (ex.: a XiongMai gun+ball) aparecem como **duas câmeras** (`-top`/`-bottom`); o app não precisa tratar nada especial — cada uma tem seu HLS/snapshot/PTZ.
- **Luz automática 18:00→05:00** roda **na API**, não na câmera (o `WorkPeriod` do
  firmware não controla o LED branco). Ela só age **na virada** do horário, então um
  `POST /lamp?state=on|off` feito pelo app continua valendo até o próximo evento —
  o agendador não fica reimpondo o estado por cima do usuário.
- Todos os endpoints retornam JSON com `detail` em erros (400/404/409/502). `409` = recurso não suportado por aquela câmera (ex.: PTZ numa fixa).
