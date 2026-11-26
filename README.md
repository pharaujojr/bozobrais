<p align="center"><img src="web/logo.svg" width="80" alt=""></p>
<h1 align="center">BozoBráis</h1>

<p align="center"><b>Monitoramento de câmeras sem nuvem: XiongMai (iCSee / XMEye) com todos os recursos e qualquer câmera RTSP.</b><br>
Ao vivo no navegador e no celular, gravação contínua com linha do tempo, PTZ, luz, áudio bidirecional e transcodificação na GPU, tudo num <code>docker compose</code>.</p>

---

## Por que existe

As câmeras Wi-Fi baratas, que vêm com o app **iCSee** ou **XMEye**, têm um hardware ótimo pelo preço, mas:
- dependem de uma nuvem de fora para funcionar;
- gravam num cartão SD que some junto com a câmera;
- o app é lento e cheio de propaganda.

O BozoBráis fala o protocolo nativo delas (**DVRIP/Sofia**, porta 34567) direto na sua rede. A câmera pode ficar **sem acesso à internet** e você continua com vídeo ao vivo, gravações no seu disco e todos os controles.

> [!WARNING]
> **Coloque as câmeras numa VLAN sem acesso à internet (WAN).** Câmeras XiongMai, como a maioria das câmeras baratas, vêm de fábrica com telnet ou senha vazia, firmware que raramente recebe correção e conexões constantes para servidores P2P no exterior. O BozoBráis não precisa que elas saiam para a internet. Isole-as por garantia: veja [Isolando as câmeras](#isolando-as-câmeras).

## Câmeras compatíveis

### ✅ Suporte completo: XiongMai (DVRIP / Sofia)

São câmeras com chipset e firmware **XiongMai (XM)**, vendidas com dezenas de marcas genéricas. Para reconhecer uma:
- **App oficial:** iCSee, XMEye, XMEye Pro ou FaceEye;
- **Portas abertas:** 34567 (DVRIP) e 554 (RTSP); muitas também abrem 8899 (ONVIF);
- **Modelos típicos:** câmeras Wi-Fi de área externa e PTZ (as "bolinhas" e as "dome"), e as **dual-lens** (lente fixa + PTZ empilhadas, vendidas como "gun + ball" ou "câmera dupla").

| Recurso | Como funciona |
|---|---|
| Auto-detecção | Informe IP, usuário e senha e ele lê modelo, firmware, PTZ, luz, áudio e resolução |
| Vídeo ao vivo | DVRIP nativo (ou RTSP, se preferir) |
| **PTZ** | ONVIF `ContinuousMove`, com o botão de pressionar e segurar |
| **Luz branca** | Ligar, desligar e modo inteligente, com **agenda** (ex.: 18:00 às 05:00) |
| **Falar pela câmera** | Áudio bidirecional: grava no navegador ou no celular e toca no alto-falante (G.711) |
| **Multi-sensor** | Câmeras de duas lentes empilhadas viram **duas câmeras** separadas, com recorte feito dentro da GPU |

**Testada em:** X6C-WEQ, uma dual-lens Wi-Fi com dois sensores de 2304×1296 empilhados, em H.265, com PTZ, luz branca, IR e alto-falante.

> **Diferenças conhecidas entre modelos:** o pan pode vir espelhado. O modo `Auto` da luz pode preferir o IR e não acender o LED. E o firmware às vezes aceita qualquer valor sem validar. Há detalhes medidos em [docs/NOTAS-XIONGMAI.md](docs/NOTAS-XIONGMAI.md).

### ✅ Qualquer câmera RTSP: Intelbras, Hikvision, Dahua, Tapo...

No site, em **Adicionar câmera → Outra marca (RTSP)**:
1. Cole a URL principal e, se houver, a do substream.
2. Clique em **Testar conexão**: o sistema mostra a resolução e o codec e diz se vai decodificar na GPU.
3. Se a câmera tiver PTZ, abra **PTZ por ONVIF** e informe porta, perfil e login.

Nesse modo há vídeo ao vivo, gravação, linha do tempo, snapshot e divisão de câmeras com duas lentes. Luz e áudio bidirecional são exclusivos da XiongMai. A câmera abre **uma conexão só** (o go2rtc repassa para todos os streams), o que importa porque modelos baratos costumam aceitar só 2 ou 3 sessões simultâneas.

| Marca | URL principal típica | Substream |
|---|---|---|
| Intelbras / Dahua | `rtsp://user:senha@IP:554/cam/realmonitor?channel=1&subtype=0` | `subtype=1` |
| Hikvision | `rtsp://user:senha@IP:554/Streaming/Channels/101` | `.../102` |
| Tapo (TP-Link) | `rtsp://user:senha@IP:554/stream1` | `stream2` |
| Reolink | `rtsp://user:senha@IP:554/h264Preview_01_main` | `..._sub` |

> Senhas com `@`, `:` ou `/` precisam ser codificadas na URL (`@` vira `%40`).

### Codec e GPU

| Câmera envia | `USE_GPU=1` (NVIDIA) | `USE_GPU=0` (CPU) |
|---|---|---|
| **H.265 / HEVC** (a maioria das XiongMai recentes) | ✅ NVDEC (`hevc_cuvid`) → NVENC, quase sem CPU | ✅ libx264, pesado com várias câmeras |
| **H.264** | ✅ NVDEC (`h264_cuvid`) → NVENC | ✅ |
| Outros (MJPEG...) | cai automaticamente para a CPU | ✅ |

O codec de cada câmera é detectado no cadastro. Com GPU, uma GTX 1050 dá conta de várias câmeras com folga.

## O que você ganha

- 📺 **Ao vivo com baixa latência:** o player tenta MSE (cerca de 0,5 s, funciona até pelo túnel), depois WebRTC (cerca de 0,3 s, na LAN) e por fim LL-HLS (cerca de 1,5 s, funciona em qualquer lugar), e troca sozinho.
- 🎞️ **Gravação contínua com linha do tempo:** uma régua por dia, reprodução que atravessa os arquivos sem emenda, quadro de qualquer instante e download.
- 💾 **Teto de disco rígido:** o pruner apaga os segmentos mais antigos para nunca passar de `MAX_STORAGE_GB`.
- 📱 **API REST para app mobile:** a especificação completa para um app Flutter está em [FLUTTER_APP.md](FLUTTER_APP.md). O site também é PWA (pode ser instalado na tela inicial).
- 🔐 **Login único:** cookie de sessão assinado no site e `X-API-Key` no app.
- 🩺 **Watchdog com aviso no Telegram:** destrava conexões travadas do go2rtc e avisa quando uma câmera cai.
- 🌍 **Acesso remoto** por Cloudflare Tunnel, abrindo **uma porta só** ([CLOUDFLARE.md](CLOUDFLARE.md)).

## Arquitetura

```
câmera --dvrip/rtsp--> go2rtc (<id>_sub · <id>_main)
                     |
                     +-- v_<id>_hd    main -> recorte -> 720p   h264_nvenc + AAC
                     +-- v_<id>_lo    sub  -> recorte -> 360p   h264_nvenc + AAC  (miniaturas)
                     +-- v_<id>_rec   sub  -> sensor inteiro    h264_nvenc + AAC  (gravação)
                     |
      MSE / WebRTC <-+  go2rtc, proxiado pela API na mesma origem
                     |
       MediaMTX <----+  só remux (copy): live_<id> -> LL-HLS · cam_<id> -> disco
```

- **go2rtc:** fala DVRIP ou RTSP com a câmera (uma conexão só) e roda os `ffmpeg`. O recorte e o redimensionamento acontecem **dentro do decodificador** (`hevc_cuvid`/`h264_cuvid -crop/-resize`), então o quadro nunca passa pela CPU.
- **MediaMTX:** só remux: LL-HLS, WebRTC e gravação em segmentos de 10 minutos.
- **api (FastAPI):**
  - serve o site e a API REST;
  - faz proxy de HLS e WebSocket;
  - monta a linha do tempo e roda o pruner;
  - **gera o `go2rtc/go2rtc.yaml`** a partir de `data/cameras.json`, por isso esse arquivo não deve ser editado à mão.

## Instalação

**Requisitos:**
- Linux com Docker e Compose;
- para usar GPU: placa NVIDIA com o [NVIDIA Container Toolkit](https://github.com/NVIDIA/nvidia-container-toolkit) instalado;
- as câmeras na mesma rede do servidor.

```bash
git clone https://github.com/pharaujojr/bozobrais.git
cd bozobrais
cp .env.example .env        # preencha SERVER_IP, AUTH_USER e API_KEY
docker compose up -d --build
```

Abra `http://IP-DO-SERVIDOR:8091`, entre com `AUTH_USER` e `API_KEY` e clique em **Adicionar câmera**. Informe o IP e o login da câmera (de fábrica, normalmente `admin` sem senha) e ele detecta o resto.

> **Sem GPU NVIDIA:** remova as linhas `runtime: nvidia` do `docker-compose.yml` e defina `USE_GPU=0` no `.env`.

### Isolando as câmeras

Faça isso **sempre**, mesmo que a rede de casa pareça segura:

1. **Crie uma VLAN só para as câmeras**, ex.: `192.168.50.0/24`, em um SSID separado se elas forem Wi-Fi.
2. **Bloqueie a saída da VLAN para a WAN** no firewall do roteador. Nenhum pacote das câmeras deve sair para a internet.
3. **Libere só o servidor do BozoBráis** a falar com a VLAN, nas portas 34567 (DVRIP), 554 (RTSP) e 8899 (ONVIF). O resto da sua rede (celulares, PCs) não precisa enxergar as câmeras: todo mundo assiste pelo BozoBráis.
4. **Relógio:** sem internet, a câmera perde o NTP. Aponte o NTP dela para o roteador ou para o servidor. As gravações usam o relógio do servidor, então isso afeta só a marca d'água da imagem.
5. **Troque a senha de fábrica** (`admin` sem senha) e, se o modelo tiver, desligue o P2P/"Cloud" no menu da câmera.

Exemplo no MikroTik (VLAN das câmeras = `vlan-cam`, servidor = `192.168.1.10`):

```
/ip firewall filter
add chain=forward in-interface=vlan-cam out-interface-list=WAN action=drop comment="cameras sem internet"
add chain=forward src-address=192.168.1.10 out-interface=vlan-cam action=accept comment="so o servidor fala com as cameras"
add chain=forward out-interface=vlan-cam connection-state=new action=drop
```

No OpenWrt, pfSense/OPNsense ou UniFi a ideia é a mesma: uma zona ou rede para as câmeras **sem encaminhamento para a WAN** e uma regra liberando só o IP do servidor.

### Watchdog (opcional)

```bash
./setup-telegram.sh <token-do-BotFather>    # grava data/telegram.conf
sudo cp systemd/cam-watchdog.* /etc/systemd/system/   # ajuste o ExecStart para a sua pasta
sudo systemctl enable --now cam-watchdog.timer
```

## API (base `/api`)

| Método | Rota | Descrição |
|---|---|---|
| GET | `/health` | Status (sem autenticação) |
| GET | `/storage` | Uso de disco comparado ao teto |
| GET / POST | `/cameras` | Lista e adiciona |
| POST | `/cameras/probe` | Auto-detecta uma XiongMai (`{ip,user,pw}`) |
| POST | `/cameras/probe-rtsp` | Testa uma câmera RTSP (`{rtsp_main, rtsp_sub}`): codec, resolução, GPU |
| PATCH / DELETE | `/cameras/{id}` | Renomeia e remove |
| POST | `/cameras/{id}/split?layout=auto` | Divide uma câmera multi-sensor |
| GET | `/cameras/{id}/snapshot` | JPEG atual |
| GET / POST | `/cameras/{id}/ptz?dir=up` · `/ptz/start` · `/ptz/stop` | PTZ |
| GET / POST | `/cameras/{id}/lamp?state=on\|off\|smart` · `/lamp/schedule` | Luz e agenda |
| POST | `/cameras/{id}/talk` | Toca uma mensagem de voz no alto-falante |
| GET | `/cameras/{id}/timeline/days` · `/timeline?date=` · `/playback?t=` · `/frame?t=` | Gravações |

Autenticação: cookie de sessão (site), `X-API-Key`, `?key=`, `Bearer` ou HTTP Basic. Streams: MSE `/ws?src=v_<id>_hd` e HLS `/hls/live_<id>/index.m3u8`.

## Operação

```bash
docker compose up -d --build     # o app.py vai DENTRO da imagem: só 'restart' não aplica mudanças
docker compose logs -f api
nvidia-smi                       # conferir NVDEC/NVENC em uso
```

## Licença

O código está sob a **[licença MIT](LICENSE)**. Os componentes de terceiros mantêm as licenças próprias (ver [CREDITS.md](CREDITS.md)).

Não tem relação com a XiongMai, com os apps iCSee e XMEye nem com os fabricantes citados. Use só em câmeras que são suas.

## Créditos

O trabalho pesado é do [go2rtc](https://github.com/AlexxIT/go2rtc) (que fala DVRIP de forma nativa), do [MediaMTX](https://github.com/bluenviron/mediamtx), do [FFmpeg](https://ffmpeg.org/), do [hls.js](https://github.com/video-dev/hls.js) e de quem fez a engenharia reversa do protocolo XiongMai. Lista completa em **[CREDITS.md](CREDITS.md)**.
