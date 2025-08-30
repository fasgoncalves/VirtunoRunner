# Virtuno Runner — README

Este README resume **instalação**, **uso** e **resolução de problemas** do Virtuno Runner
com **Render & Play de MIDI no servidor** e **MP3/WAV intocados**.

---

## ✨ Visão Geral

- **MIDI (Render & Play)**: o servidor converte `.mid` em **WAV/MP3** usando **FluidSynth + SoundFont**  
  (fallback **Timidity**), e toca num `<audio>` nativo no browser.
- **MP3/WAV/OGG**: reprodução nativa (sem alterações).
- **Interface NiceGUI**: lista de ficheiros (Pré‑visualizar, Download, Tocar), painel de preview e log de render.

> O render usa hashing do nome do `.mid` + caminho do SoundFont para **reaproveitar** ficheiros já renderizados.

---

## 📦 Requisitos

### Sistema (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install -y fluidsynth fluid-soundfont-gm timidity ffmpeg
```

### Python
```bash
pip install nicegui
```

> Se usares `venv`, ativa-o antes do `pip install`.

---

## 🚀 Arranque Rápido

1) Coloca os teus `.mid`, `.mp3`, `.wav` em:
   ```bash
   export EXECUTOR_OUTPUT_DIR="/home/username/Binaries/chatgpt_outputs"
   ```
   (Cria a pasta se não existir.)

2) (Opcional) Aponta para o teu **SoundFont** preferido:
   ```bash
   export GM_SF2="/usr/share/sounds/sf2/FluidR3_GM.sf2"
   ```

3) (Opcional) Porta e segredo de sessão:
   ```bash
   export EXECUTOR_PORT=2020
   export EXECUTOR_SESSION_SECRET="virtuno-secret"
   ```

4) Corre a app:
   ```bash
   python3 VirtunoRunner.py
   ```

5) Abre no browser: `http://localhost:2020` (ajusta a porta se alteraste).

---

## 🧭 Como Usar

- **Lista de ficheiros**
  - **Pré-visualizar**: abre no painel da direita.
  - **Download**: baixa o ficheiro (via rota `/download/{ficheiro}`).
  - **Tocar** (apenas MP3/WAV): toca imediatamente com `<audio>` nativo.
  - **Render & Play** (apenas MIDI): renderiza para WAV/MP3 no servidor e toca de seguida.

- **Painel de Pré‑visualização**
  - Funciona para imagem, vídeo, HTML, áudio (MP3/WAV) e **MIDI** com **Renderizar & Tocar**.
  - Se o render falhar, aparece um **Log** com comandos e erros.

---

## 🔧 Variáveis de Ambiente

| Variável                 | Default                                            | Descrição |
|--------------------------|-----------------------------------------------------|-----------|
| `EXECUTOR_OUTPUT_DIR`    | `/home/username/Binaries/chatgpt_outputs`           | Pasta onde a app lê/escreve ficheiros (MID/MP3/WAV). |
| `EXECUTOR_PORT`          | `2020`                                              | Porta HTTP da aplicação NiceGUI. |
| `EXECUTOR_SESSION_SECRET`| `virtuno-secret`                                    | Segredo para a sessão NiceGUI. |
| `EXECUTOR_TOKEN`         | _vazia_                                             | Se definido, acrescenta `?token=...` às URLs de download. |
| `GM_SF2`                 | _auto-deteta SF2 comuns_                            | Caminho para o SoundFont **.sf2** a usar pelo FluidSynth. |

---

## 🛠️ Como Funciona o Render de MIDI

1. Tentativa com **FluidSynth** + `GM_SF2` (ou SF2 comuns do sistema).  
   - Comando base:  
     ```bash
     fluidsynth -ni <SF2> <MIDI> -F <OUT.wav> -r 44100 -g 0.9
     ```
2. Se falhar, tenta **Timidity** para gerar **WAV**:
   ```bash
   timidity <MIDI> -Ow -o <OUT.wav> -s 44100
   ```
3. Se existir **FFmpeg**, converte também para **MP3** de qualidade boa:
   ```bash
   ffmpeg -y -i <OUT.wav> -codec:a libmp3lame -qscale:a 2 <OUT.mp3>
   ```
4. O browser recebe o **/download/<ficheiro>** e toca.

> O ficheiro de saída inclui um **hash** do caminho `.mid` + SF2, para evitar colisões e **reaproveitar** renderizações.

---

## 📱 Notas Desktop / Android

- **Desbloqueio de áudio**: em Android/iOS, o primeiro play pode exigir um **toque** do utilizador (gesto). Os botões da UI já tratam disso.
- **Download externo**: botões de “Download” abrem o ficheiro no player do sistema, se preferires tocar fora do browser.
- **CORS**: a rota `/download` já inclui `Access-Control-Allow-Origin: *` (útil para apps híbridas).

---

## 🧪 Dicas de Qualidade & Performance

- **SoundFont**: muda o `GM_SF2` para um SF2 com melhor timbre — impacta muito a qualidade final.
- **Taxa de amostragem**: em ambientes de recursos limitados, baixa `-r 44100` para `-r 22050` no comando do FluidSynth para acelerar (com perda de qualidade).
- **MP3 mais leve**: troca `-qscale:a 2` por `4` no FFmpeg.
- **Ganho**: ajusta `-g 0.9` no FluidSynth para controlar volume/clipping.

---

## 🧯 Resolução de Problemas

### “Falha a renderizar (ver log)”
- Abre o **Log** no painel. Procura pelas linhas que começam com `$`:
  - `$ fluidsynth …`: se aparecer “command not found” → `sudo apt-get install fluidsynth`.
  - “sf2 não encontrado” → define `GM_SF2="/caminho/para/teu.sf2"` ou instala `fluid-soundfont-gm`.
  - `$ timidity …`: se aparecer “command not found” → `sudo apt-get install timidity`.
  - Se faltar FFmpeg para MP3: `sudo apt-get install ffmpeg` (continua a tocar WAV).

### Não sai som no browser
- Garante um **gesto de utilizador** (carregar no botão) antes do primeiro `play()`.
- Verifica se o ficheiro `/download/...` abre em nova aba (deve reproduzir).

### Não vejo os ficheiros na lista
- Confere o diretório:
  ```bash
  echo "$EXECUTOR_OUTPUT_DIR"
  ls -lah "$EXECUTOR_OUTPUT_DIR"
  ```
  (A app só lista ficheiros que **não** começam por `.`).

---

## 🔒 Notas de Segurança

- A rota `/download/{ficheiro}` **só** serve ficheiros dentro de `EXECUTOR_OUTPUT_DIR` (verificação de `basename` e caminho).  
- Opcional: define `EXECUTOR_TOKEN` para acrescentar `?token=...` às URLs de download.
- Os headers de resposta incluem `Cache-Control: no-store`.

---

## 🧩 Serviço (opcional) com systemd

Cria `/etc/systemd/system/virtuno.service` (ajusta caminhos/variáveis):

```
[Unit]
Description=Virtuno Runner
After=network.target

[Service]
Environment=EXECUTOR_OUTPUT_DIR=/home/username/Binaries/chatgpt_outputs
Environment=EXECUTOR_PORT=2020
Environment=GM_SF2=/usr/share/sounds/sf2/FluidR3_GM.sf2
WorkingDirectory=/home/username/Binaries
ExecStart=/usr/bin/python3 /home/username/Binaries/VirtunoRunner.py
Restart=on-failure
User=username
Group=username

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable virtuno --now
sudo systemctl status virtuno -n 50
```

---

## ✅ Checklist Rápido

- [ ] `pip install nicegui`  
- [ ] `apt install fluidsynth fluid-soundfont-gm timidity ffmpeg`  
- [ ] `export GM_SF2="/usr/share/sounds/sf2/FluidR3_GM.sf2"`  
- [ ] `export EXECUTOR_OUTPUT_DIR="/home/fgoncalves/Binaries/chatgpt_outputs"`  
- [ ] `python3 VirtunoRunner.py`  
- [ ] Abrir `.mid` → **Renderizar & Tocar**.

---

By: Francisco Gonçalves in 30 August 2025 (c) All rights reserved
e-mail : francis.goncalves@gmail.com
---------------------------------------------------------------------------------
