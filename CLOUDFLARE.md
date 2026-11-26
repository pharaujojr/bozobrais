# Cloudflared — expor em `cameras.seudominio.com`

## O que expor

**Apenas uma porta: `localhost:8091`.**

Tudo passa por ela — site, API, snapshots, gravações e o **vídeo ao vivo (HLS via proxy)**.
Você **não** precisa expor 8888/8889/8554/8189. WebRTC (baixa latência) fica só na LAN;
pelo túnel o player usa HLS automaticamente.

| Serviço | Porta | Expor no cloudflared? |
|---|---|---|
| **App/API/site + HLS** | **8091** | ✅ **sim (a única)** |
| MediaMTX HLS interno | 8888 | ❌ (a API já proxia em `/hls`) |
| MediaMTX WebRTC | 8889 + 8189/udp | ❌ (UDP não passa no CF; só LAN) |
| RTSP | 8554 | ❌ |

## Configuração do túnel

```bash
# 1) autentica e cria o túnel
cloudflared tunnel login
cloudflared tunnel create cameras

# 2) aponta o DNS
cloudflared tunnel route dns cameras cameras.seudominio.com
```

`~/.cloudflared/config.yml`:
```yaml
tunnel: cameras
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: cameras.seudominio.com
    service: http://localhost:8091
  - service: http_status:404
```

```bash
# 3) roda (ou instala como serviço p/ subir no boot)
cloudflared tunnel run cameras
#   ou:  sudo cloudflared service install && sudo systemctl enable --now cloudflared
```

Pronto: acesse **https://cameras.seudominio.com** → login com `AUTH_USER` / `API_KEY` do `.env`.
O app mobile usa a mesma URL com header `X-API-Key: <API_KEY>`.

## Segurança

- **Senha do app** (`AUTH_USER`/`API_KEY`) já protege tudo (site + API + streams via `/hls`). Troque em `.env (na pasta do projeto)` (`AUTH_USER` / `API_KEY`) e `docker compose up -d`.
- **Recomendado:** ative também **Cloudflare Access** no hostname (Zero Trust → Access → Applications) com um PIN por e-mail. Vira uma 2ª camada antes mesmo de chegar no app.
- Como o cloudflared fala com a API a partir de `localhost`, o IP de origem chega como 127.0.0.1 — a proteção real é a senha do app + (opcional) Cloudflare Access.
