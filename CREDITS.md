# Créditos

O BozoBráis é a orquestração (API, site, regras de gravação e controle) em volta destes projetos. O mérito do trabalho pesado é deles.

## Mídia

| Projeto | Papel aqui | Licença |
|---|---|---|
| [go2rtc](https://github.com/AlexxIT/go2rtc), de Alexey Khit | Fala DVRIP/RTSP com as câmeras, MSE/WebRTC e áudio de volta (backchannel) | MIT |
| [MediaMTX](https://github.com/bluenviron/mediamtx) (bluenviron) | Remux para LL-HLS e WebRTC, gravação em disco | MIT |
| [FFmpeg](https://ffmpeg.org/) | Transcodificação (NVDEC/NVENC ou libx264), reprodução e quadros | LGPL-2.1+ / GPL (builds das imagens) |
| [hls.js](https://github.com/video-dev/hls.js) 1.5.17 (`web/hls.min.js`) | Player LL-HLS no navegador | Apache-2.0 |
| [NVIDIA Container Toolkit](https://github.com/NVIDIA/nvidia-container-toolkit) | GPU dentro dos containers | Apache-2.0 |

## Aplicação

| Projeto | Licença |
|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT |

## Protocolo XiongMai

O cliente DVRIP/Sofia (login com hash, `SystemInfo`, `SystemFunction`, PTZ, luz e `OPTalk`) foi escrito com base na engenharia reversa publicada pela comunidade, em especial o [python-dvr](https://github.com/NeiroNx/python-dvr) (MIT) e o trabalho do [OpenIPC](https://openipc.org/) sobre o firmware XiongMai.

## Serviços

- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) ([cloudflared](https://github.com/cloudflare/cloudflared), Apache-2.0): acesso remoto sem abrir portas.
- [Telegram Bot API](https://core.telegram.org/bots/api): avisos do watchdog.

---

O código próprio deste repositório é MIT ([LICENSE](LICENSE)); cada projeto acima segue a sua licença. Esqueci alguém? Abra uma issue ou um PR.
