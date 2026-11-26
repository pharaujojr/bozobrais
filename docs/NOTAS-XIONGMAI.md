# Notas de campo: câmeras XiongMai

Comportamentos medidos na prática (modelo de referência: X6C-WEQ, dual-lens Wi-Fi).
Outros modelos podem variar; contribuições com outros modelos são bem-vindas.

## Luz branca (lâmpada)

- `state=on` usa `WorkMode=KeepOpen`, que acende o LED branco na hora. `off` usa `Close`, `smart` usa `Intelligent` e `auto` usa `Auto`.
- **`Auto` não acende** em câmeras dual-light: no escuro elas preferem o IR.
- A câmera **aceita qualquer string sem validar**: devolve `Ret=100` e ecoa o valor na releitura. Nem o retorno da API nem o `GET /lamp` provam que a luz acendeu; só medindo o brilho da cena. Medido: `Close` 69, `KeepOpen` 113, `Auto`/`Manual`/`Open` 84.

## Agenda da luz

- Roda na API (`lamp_sched_loop`), não na câmera: o `WorkPeriod` do firmware **não** controla o LED branco. Testado dentro da janela, ele continuou apagado.
- Dispara só **na virada** do horário. Ligar ou desligar pelo app continua valendo até o próximo evento.
- Fica sempre na câmera-pai: os recortes (`rec_ref`) dividem o mesmo LED físico.

## PTZ

- Usa ONVIF `ContinuousMove` (porta 8899, perfil `000`).
- O pan é espelhado no modelo de referência.

## DVRIP preso (Ret=205)

O DVRIP não libera os slots de sessão quando o host reinicia sem fechar as conexões. Nesse estado ele recusa todo login (`Ret=205`), inclusive de usuário inexistente, até a câmera perder energia.

Contorno: `video_source: "rtsp"` na câmera tira o vídeo do DVRIP. O custo é perder o áudio bidirecional no stream base.

## Câmeras multi-sensor

Duas lentes empilhadas chegam como um quadro só (ex.: 2304×2592 = 2 × 2304×1296). `POST /cameras/{id}/split?layout=auto` cria duas câmeras (`-top` e `-bottom`) recortadas dentro do decodificador da GPU, sem custo extra relevante. A gravação continua sendo do sensor inteiro, compartilhada.
