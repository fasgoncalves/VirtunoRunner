import hashlib
import subprocess
import shutil
import json
# -*- coding: utf-8 -*-
"""
VirtunoRunner (FULL • UMD fix) — NiceGUI executor com preview e MIDI
- Corrige "Midi is not a constructor" no browser: carrega UMD de @tonejs/midi (window.Midi)
- Desbloqueio de áudio no clique (Tone.start/resume) e fallback para ESM se necessário
- Android: botão "Abrir em app" (Web Share API + intent://) e download com token na query
- /download serve audio/midi com no-store e CORS opcional
"""

from nicegui import ui, app, events
from fastapi import Response
from fastapi.responses import FileResponse
import os, sys, time, uuid, shlex, shutil, platform, subprocess, mimetypes, re, json, hashlib

# ==========================
# CONFIGURAÇÃO
# ==========================
OUTPUT_DIR = os.environ.get('EXECUTOR_OUTPUT_DIR', '/home/fgoncalves/Binaries/chatgpt_outputs')
TMP_DIR     = os.environ.get('EXECUTOR_TMP_DIR', '/tmp/remote-executor')
PORT        = int(os.environ.get('EXECUTOR_PORT', '2020'))

# assets locais opcionais (CodeMirror, Tone/Midi)
VENDOR_DIR  = os.environ.get('EXECUTOR_VENDOR_DIR', '/home/fgoncalves/Binaries/codemirror')
if os.path.isdir(VENDOR_DIR):
    app.add_static_files('/codemirror', VENDOR_DIR)

# sessão e auth
SESSION_SECRET = os.environ.get('EXECUTOR_SESSION_SECRET', 'change-me-please')
REQUIRE_LOGIN  = os.environ.get('EXECUTOR_REQUIRE_LOGIN', '1') not in ('0','false','False')
ADMIN_USER     = os.environ.get('EXECUTOR_USER', 'admin')
ADMIN_PASS     = os.environ.get('EXECUTOR_PASS', 'secret')

# token para permitir acesso a /download sem cookies (para apps externas)
ACCESS_TOKEN   = os.environ.get('EXECUTOR_TOKEN', '').strip()

# limites e preferências
PY_TIMEOUT       = int(os.environ.get('EXECUTOR_PY_TIMEOUT', '180'))
SH_TIMEOUT       = int(os.environ.get('EXECUTOR_SH_TIMEOUT', '120'))
STREAM_INTERVAL  = 0.1
MAX_OUTPUT_CHARS = 200_000

# tipos de artefactos suportados
mimetypes.add_type('audio/midi', '.mid')
mimetypes.add_type('audio/midi', '.midi')
mimetypes.add_type('audio/x-midi', '.mid')
mimetypes.add_type('audio/x-midi', '.midi')
mimetypes.add_type('audio/mpeg', '.mp3')
mimetypes.add_type('audio/wav', '.wav')
ARTIFACT_EXTS   = {'.png','.jpg','.jpeg','.svg','.pdf','.html','.htm','.gif','.mp4','.webm','.avi','.mov','.mid','.midi','.py','.php','.java','.js','.zip','.txt','.log'}
PREVIEW_IMG_EXT = {'.png','.jpg','.jpeg','.gif','.svg'}
PREVIEW_HTML_EXT= {'.html','.htm'}
PREVIEW_VIDEO_EXT={'.mp4','.webm'}
PREVIEW_AUDIO_EXT = {'.mp3','.wav','.ogg'}
PREVIEW_MIDI_EXT= {'.mid','.midi'}
PREVIEW_CODE_EXT= {'.txt','.log','.php','.java','.js'}

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# Expor directoria pública
app.add_static_files('/files', OUTPUT_DIR)

# ==========================
# ESTADO por cliente
# ==========================
CLIENTS = {}      # cid -> state
CODE_STORE = {}   # cid -> last-code pulled from editor

def ensure_state(cid: str):
    if cid not in CLIENTS:
        CLIENTS[cid] = {
            'proc': None,
            'pty_master_fd': None,
            'use_pty': True,
            'timer': None,
            'timeout_deadline': None,
            'stopped': False,
            'start_time': None,
            'output_widget': None,
            'debug_widget': None,
            'script_path': None,
            'runner_path': None,
            'debug_events': [],
        }
    return CLIENTS[cid]

# ==========================
# AUTH
# ==========================
def session_ok() -> bool:
    try:
        if app.storage.user.get('auth') is True:
            return True
    except Exception:
        pass
    if ACCESS_TOKEN:
        try:
            token = ui.context.request.query_params.get('token', '')
            if token == ACCESS_TOKEN:
                app.storage.user['auth'] = True
                app.storage.user['user'] = 'token'
                return True
        except Exception:
            pass
    return False

def gate_ui() -> bool:
    if REQUIRE_LOGIN and not session_ok():
        ui.navigate.to('/login')
        return False
    return True

# ==========================
# AUXILIARES
# ==========================
def debug_log(msg: str):
    cid = ui.context.client.id
    st = ensure_state(cid)
    ts = time.strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print('[DEBUG]', line)
    st['debug_events'].append(line)
    st['debug_events'] = st['debug_events'][-600:]
    if st['debug_widget'] is not None:
        try:
            st['debug_widget'].value = '\n'.join(st['debug_events'])
            st['debug_widget'].update()
        except Exception:
            pass

def build_download_url(filename: str) -> str:
    safe = os.path.basename(filename)
    base = f'/download/{safe}'
    return base + (f'?token={ACCESS_TOKEN}' if ACCESS_TOKEN else '')

def js_send_code(cid: str) -> str:
    js = r"""
(function(){
  const ed = window._cm_editor;
  const code = ed ? ed.getValue() : ((document.getElementById('code-editor')||{}).value||'');
  fetch('/__code__', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({cid: __CID__, code: code})
  }).catch(()=>{{}});
})();
"""
    return js.replace('__CID__', json.dumps(cid))

# ==========================
# ROTAS
# ==========================
@ui.page('/')
def index_page():
    dest = '/executor' if (not REQUIRE_LOGIN or session_ok()) else '/login'
    ui.timer(0.01, once=True, callback=lambda: ui.navigate.to(dest))

@ui.page('/login')
def login_page():
    with ui.card().classes('max-w-md mx-auto mt-20'):
        ui.label('Virtuno Runner • Login').classes('text-xl font-semibold')
        user = ui.input('Utilizador').classes('w-full')
        pw   = ui.input('Senha', password=True, password_toggle_button=True).classes('w-full')
        msg  = ui.label('').classes('text-negative')
        def do_login():
            u = (user.value or '').strip(); p = pw.value or ''
            if u == ADMIN_USER and p == ADMIN_PASS:
                app.storage.user['auth'] = True
                app.storage.user['user'] = u
                ui.notify('Sessão iniciada', color='positive')
                dest = f'/executor?token={ACCESS_TOKEN}' if ACCESS_TOKEN else '/executor'
                ui.navigate.to(dest)
            else:
                msg.text = 'Credenciais inválidas'; ui.notify('Login falhou', color='negative')
        with ui.row().classes('justify-end w-full mt-2'):
            ui.button('Entrar', on_click=do_login, color='primary')

@ui.page('/logout')
def logout_page():
    try: app.storage.user.clear()
    except Exception: pass
    ui.notify('Sessão terminada', color='info'); ui.navigate.to('/login')

def _guess_media_type(name: str):
    ext = os.path.splitext(name)[1].lower()
    if ext in ('.mid', '.midi'):
        return 'audio/midi'
    return None

@ui.page('/download/{filename}')
def download_page(filename: str):
    # segurança mínima
    safe = os.path.basename(filename)
    caminho = os.path.abspath(os.path.join(OUTPUT_DIR, safe))
    base = os.path.abspath(OUTPUT_DIR)
    if not caminho.startswith(base + os.sep):
        return Response('Forbidden', status_code=403)
    if not os.path.isfile(caminho):
        return Response('Not Found', status_code=404)

    media = _guess_media_type(safe)
    resp = FileResponse(caminho, filename=safe, media_type=media)
    # CORS opcional se necessário
    try:
        origin = ui.context.request.headers.get('origin')
        if origin:
            resp.headers['Access-Control-Allow-Origin'] = origin
            resp.headers['Vary'] = 'Origin'
    except Exception:
        pass
    resp.headers['Cache-Control'] = 'no-store'
    return resp



# ===== Helpers de render MIDI (apenas para o play de MIDI) =====
def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def _find_sf2() -> str | None:
    cand = []
    env_sf2 = os.environ.get('GM_SF2', '').strip()
    if env_sf2: cand.append(env_sf2)
    cand += [
        '/usr/share/sounds/sf2/FluidR3_GM.sf2',
        '/usr/share/sounds/sf3/FluidR3_GM.sf3',
        '/usr/share/sounds/sf2/TimGM6mb.sf2',
        '/usr/local/share/sounds/sf2/FluidR3_GM.sf2',
    ]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None

def _render_midi(mid_path: str) -> tuple[bool, str, str, str]:
    """(ok, url_rel, fmt, log) — renderiza .mid -> WAV/MP3 no OUTPUT_DIR e devolve URL via /download"""
    base = os.path.splitext(os.path.basename(mid_path))[0]
    try:
        sf2 = _find_sf2()
        h = hashlib.sha1((mid_path + '|' + (sf2 or '')).encode()).hexdigest()[:10]
    except Exception:
        sf2 = _find_sf2(); h = 'nohash'
    wav = os.path.join(OUTPUT_DIR, f'{base}.{h}.wav')
    mp3 = os.path.join(OUTPUT_DIR, f'{base}.{h}.mp3')
    log_parts: list[str] = []

    def run_cmd(cmd, timeout=180):
        log_parts.append('$ ' + ' '.join(cmd))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.stdout: log_parts.append(r.stdout)
            if r.stderr: log_parts.append('\n' + r.stderr)
            return r.returncode == 0
        except Exception as e:
            log_parts.append(f'[EXCEÇÃO] {e}')
            return False

    if _have('fluidsynth') and sf2:
        if run_cmd(['fluidsynth','-ni', sf2, mid_path, '-F', wav, '-r','44100','-g','0.9']):
            if os.path.isfile(wav) and os.path.getsize(wav) > 1000:
                if _have('ffmpeg') and run_cmd(['ffmpeg','-y','-i', wav, '-codec:a','libmp3lame','-qscale:a','2', mp3], timeout=120):
                    if os.path.isfile(mp3) and os.path.getsize(mp3) > 1000:
                        url = build_download_url(os.path.basename(mp3)) if 'build_download_url' in globals() else f"/download/{os.path.basename(mp3)}"
                        return True, url, 'mp3', ''.join(log_parts)
                url = build_download_url(os.path.basename(wav)) if 'build_download_url' in globals() else f"/download/{os.path.basename(wav)}"
                return True, url, 'wav', ''.join(log_parts)

    if _have('timidity'):
        if run_cmd(['timidity', mid_path, '-Ow', '-o', wav, '-s','44100']):
            if os.path.isfile(wav) and os.path.getsize(wav) > 1000:
                if _have('ffmpeg') and run_cmd(['ffmpeg','-y','-i', wav, '-codec:a','libmp3lame','-qscale:a','2', mp3], timeout=120):
                    if os.path.isfile(mp3) and os.path.getsize(mp3) > 1000:
                        url = build_download_url(os.path.basename(mp3)) if 'build_download_url' in globals() else f"/download/{os.path.basename(mp3)}"
                        return True, url, 'mp3', ''.join(log_parts)
                url = build_download_url(os.path.basename(wav)) if 'build_download_url' in globals() else f"/download/{os.path.basename(wav)}"
                return True, url, 'wav', ''.join(log_parts)

    tips = []
    if not _have('fluidsynth'): tips.append('instala fluidsynth')
    if not sf2: tips.append('instala fluid-soundfont-gm ou define GM_SF2')
    if not _have('timidity'): tips.append('instala timidity')
    if tips: log_parts.append('[DICA] ' + '; '.join(tips))
    return False, '', '', ''.join(log_parts)

@app.post('/__code__')
async def __set_code(payload: dict):
    cid = payload.get('cid')
    code = payload.get('code', '')
    if cid:
        CODE_STORE[cid] = code
    return {'ok': True, 'len': len(code)}

@app.post('/__delete__')
async def __delete_file(payload: dict):
    if REQUIRE_LOGIN and not session_ok():
        return {'ok': False, 'error': 'forbidden'}
    name = payload.get('filename') or ''
    safe = os.path.basename(name)
    base = os.path.abspath(OUTPUT_DIR)
    path = os.path.abspath(os.path.join(OUTPUT_DIR, safe))
    if not path.startswith(base + os.sep):
        return {'ok': False, 'error': 'forbidden'}
    if not os.path.isfile(path):
        return {'ok': False, 'error': 'not_found'}
    try:
        os.remove(path); return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

# ==========================
# INTERFACE PRINCIPAL
# ==========================
@ui.page('/executor')
def executor_page():
    import select, pty

    if not gate_ui():
        return

    # Header
    with ui.header().classes('items-center justify-between'):
        ui.label('Executor NiceGUI · Virtuno').classes('text-lg')
        with ui.row():
            def clear_debug():
                st = ensure_state(ui.context.client.id)
                st['debug_events'].clear()
                if st['debug_widget'] is not None:
                    st['debug_widget'].value = ''; st['debug_widget'].update()
                ui.notify('Debug limpo', color='info')
            ui.button('Limpar Debug', on_click=clear_debug).props('flat')
            ui.button('Sair da sessão', on_click=lambda: ui.navigate.to('/logout')).props('flat')
        status = ui.label('Pronto').style('font-weight:600;')

    # Diálogos
    with ui.dialog() as popup, ui.card():
        popup_msg = ui.label('')
    with ui.dialog() as preview_dialog, ui.card().classes('w-[90vw] max-w-[1100px]'):
        preview_title = ui.label('Pré-visualização').classes('text-lg font-semibold')
        preview_area = ui.column().classes('w-full')
    with ui.dialog() as save_dialog, ui.card().classes('max-w-md w-full'):
        ui.label('Guardar script como .py').classes('text-lg font-semibold')
        save_name = ui.input('Nome do ficheiro (ex: experimento.py)').props('outlined dense').classes('w-full')
        ui.label('Caso omitas .py eu acrescento automaticamente.').classes('text-xs text-gray-600')
        def do_save_current_code():
            name = (save_name.value or '').strip()
            if not name:
                name = f'script-{int(time.time())}.py'
            base_name = os.path.basename(name)
            if not base_name.endswith('.py'):
                base_name += '.py'
            safe = re.sub(r'[^A-Za-z0-9_.-]', '_', base_name)
            cid = ui.context.client.id
            CODE_STORE.pop(cid, None)
            ui.run_javascript(js_send_code(cid))
            attempts={'n':0}
            def poll_save():
                attempts['n']+=1
                code = CODE_STORE.pop(cid, None)
                if code is not None:
                    dst = os.path.join(OUTPUT_DIR, safe)
                    try:
                        with open(dst,'w',encoding='utf-8') as f: f.write(code)
                        ui.notify(f'Script guardado em {safe}', color='positive')
                    except Exception as e:
                        ui.notify(f'Erro a guardar: {e}', color='negative')
                    save_dialog.close(); atualizar_lista(); waiter.cancel()
                elif attempts['n']>=100:
                    ui.notify('Timeout a capturar código', color='warning'); save_dialog.close(); waiter.cancel()
            waiter = ui.timer(0.05, poll_save)
        with ui.row().classes('justify-end w-full mt-2 gap-2'):
            ui.button('Cancelar', on_click=lambda: save_dialog.close())
            ui.button('Guardar', on_click=do_save_current_code, color='primary')

    # DEBUG
    with ui.expansion('DEBUG', value=True).classes('w-full'):
        debug_box = ui.textarea().props('readonly outlined dense').style('width:100%;height:200px;')

    # HEAD assets (CodeMirror + Tone + Midi + player)
    ui.add_head_html(r"""
<link rel="stylesheet" href="/codemirror/codemirror.min.css">
<link rel="stylesheet" href="/codemirror/eclipse.min.css">
<link rel="stylesheet" href="/codemirror/dracula.min.css">
<script src="/codemirror/codemirror.min.js"></script>
<script src="/codemirror/python.min.js"></script>
<style>
  .CodeMirror{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,Liberation Mono,monospace;font-size:14px;height:420px;border:1px solid #ddd;border-radius:6px}
  @media (max-width:768px){.CodeMirror{height:320px}}
  .preview-media{max-width:100%;max-height:74vh;border-radius:6px}
  .preview-iframe{width:100%;height:74vh;border:0;border-radius:6px}
</style>
""")

    # Tabs
    with ui.tabs().props('align="justify"') as tabs:
        tab_python = ui.tab('Python')
        tab_shell  = ui.tab('Shell')
    with ui.tab_panels(tabs, value=tab_python).classes('w-full'):

        # ---------- TAB PYTHON ----------
        with ui.tab_panel(tab_python):
            ui.label('Executar scripts Python').classes('text-md')
            def toggle_theme():
                ui.run_javascript(r"""
                  if (!window._cm_editor) return;
                  const cur = window._cm_editor.getOption('theme');
                  const next = (cur === 'dracula') ? 'eclipse' : 'dracula';
                  window._cm_editor.setOption('theme', next);
                """)
            ui.button('Tema editor', on_click=toggle_theme).props('flat').classes('q-ml-sm')
            ui.html(r"""
            <textarea id="code-editor"># -*- coding: utf-8 -*-
import os, time, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
print('Hello from the executor')
plt.plot([1,2,3],[1,4,9]); plt.title('Demo')
plt.show()  # será guardado automaticamente no OUTPUT_DIR
            </textarea>
            """)
            saida_py = ui.textarea(placeholder='Saída do programa (Python)').props('readonly outlined dense').style('width:100%;height:240px;margin-top:6px;')

            cid = ui.context.client.id
            st = ensure_state(cid)
            st['output_widget'] = saida_py
            st['debug_widget'] = debug_box

            # Ambiente
            debug_log(f'Python {platform.python_version()} on {platform.system()} {platform.release()}')
            debug_log(f'OUTPUT_DIR={OUTPUT_DIR} writable={os.access(OUTPUT_DIR, os.W_OK)}')
            debug_log(f'TMP_DIR={TMP_DIR} writable={os.access(TMP_DIR, os.W_OK)}')
            for path,label in [(OUTPUT_DIR,'OUTPUT_DIR'),(TMP_DIR,'TMP_DIR')]:
                try:
                    p = os.path.join(path,'.__executor_test'); open(p,'w').write('ok'); os.remove(p)
                    debug_log(f'{label} write test OK')
                except Exception as e:
                    debug_log(f'{label} write test FAILED: {e}')

            # inicializar CodeMirror
            ui.timer(0.1, once=True, callback=lambda: ui.run_javascript(r"""
              const ta = document.getElementById('code-editor');
              if (!ta) return;
              if (!window._cm_editor) {
                window._cm_editor = CodeMirror.fromTextArea(ta, {
                  lineNumbers: true, mode: 'python', theme: 'dracula',
                  indentUnit: 4, tabSize: 4, indentWithTabs: false,
                  smartIndent: true, matchBrackets: true,
                });
              }
            """))

            # helpers
            def _append_with_limit(widget, text: str):
                if not text: return
                v = (widget.value or '') + text
                if len(v) > MAX_OUTPUT_CHARS:
                    v = '...[truncado]...\n' + v[-MAX_OUTPUT_CHARS:]
                widget.value = v
                try: widget.update()
                except Exception: pass

            def _start_stream_timer(cid: str):
                st = ensure_state(cid)
                out = st['output_widget']

                def flush():
                    # PTY
                    if st.get('use_pty') and st.get('pty_master_fd') is not None:
                        try:
                            r,_,_ = select.select([st['pty_master_fd']], [], [], 0)
                            while r:
                                try:
                                    chunk = os.read(st['pty_master_fd'], 4096)
                                    if not chunk: break
                                    _append_with_limit(out, chunk.decode('utf-8', errors='replace'))
                                    r,_,_ = select.select([st['pty_master_fd']], [], [], 0)
                                except Exception:
                                    break
                        except Exception as e:
                            debug_log(f'flush PTY error: {e}')

                    if st['timeout_deadline'] and time.monotonic() > st['timeout_deadline'] and st['proc'] and st['proc'].poll() is None:
                        debug_log('timeout reached; terminating child')
                        try: st['proc'].terminate()
                        except Exception: pass
                        return

                    if st['proc'] and st['proc'].poll() is not None:
                        rc = st['proc'].returncode
                        debug_log(f'process exited with code {rc}')
                        _finalizar_execucao(cid, rc)
                        return

                st['timer'] = ui.timer(STREAM_INTERVAL, callback=flush)

            def _copy_new_artifacts_since(start_time: float):
                copied = []
                try:
                    for name in os.listdir(TMP_DIR):
                        src = os.path.join(TMP_DIR, name)
                        if not os.path.isfile(src) or name.startswith('.'): continue
                        if os.path.getmtime(src) + 0.001 < start_time: continue
                        _, ext = os.path.splitext(name)
                        if ext.lower() not in ARTIFACT_EXTS: continue
                        dst = os.path.join(OUTPUT_DIR, name)
                        base, ext2 = os.path.splitext(dst)
                        c=1
                        while os.path.exists(dst):
                            dst = f'{base}-copy{c}{ext2}'; c+=1
                        shutil.copy2(src, dst); copied.append(os.path.basename(dst))
                except Exception as e:
                    debug_log(f'copy artifacts error: {e}')
                if copied:
                    debug_log('copied artifacts: ' + ', '.join(copied))

            def _finalizar_execucao(cid: str, rc: int|None):
                st = ensure_state(cid)
                out = st['output_widget']

                if st['timer']: st['timer'].cancel(); st['timer']=None

                if st.get('use_pty') and st.get('pty_master_fd') is not None:
                    try:
                        while True:
                            chunk = os.read(st['pty_master_fd'], 4096)
                            if not chunk: break
                            _append_with_limit(out, chunk.decode('utf-8', errors='replace'))
                    except Exception:
                        pass

                if st.get('start_time'): _copy_new_artifacts_since(st['start_time'])

                if st['proc'] and st['proc'].poll() is None:
                    try:
                        st['proc'].terminate(); time.sleep(0.3)
                        if st['proc'].poll() is None: st['proc'].kill()
                    except Exception: pass

                if st['stopped']:
                    status.text='Execução parada'; ui.notify('Execução parada', color='warning'); popup_msg.text='Execução parada'
                elif rc == 0:
                    status.text='Execução concluída'; ui.notify('Execução concluída', color='positive'); popup_msg.text='Execução concluída com sucesso'
                else:
                    status.text=f'Terminou com erros (código {rc})'; ui.notify(f'Terminou com erros (código {rc})', color='warning'); popup_msg.text=f'Execução terminou com erros (código {rc})'
                popup.open(); atualizar_lista()

                for key in ('script_path','runner_path'):
                    p = st.get(key)
                    if p and os.path.isfile(p):
                        try: os.remove(p)
                        except Exception: pass
                        st[key] = None
                if st.get('pty_master_fd') is not None:
                    try: os.close(st['pty_master_fd'])
                    except Exception: pass
                    st['pty_master_fd'] = None
                st['proc'] = None
                st['timeout_deadline'] = None
                st['stopped'] = False
                st['start_time'] = None

            def _write_runner(wrapper_path: str):
                code = r"""
import os, sys, time, runpy
os.environ.setdefault('MPLBACKEND','Agg')
OUTPUT_DIR = os.environ.get('EXECUTOR_OUTPUT_DIR','.')

try:
    import matplotlib
    try: matplotlib.use(os.environ.get('MPLBACKEND','Agg'), force=True)
    except Exception: pass
    import matplotlib.pyplot as plt
    from matplotlib import animation as _anim_mod
    _orig_show = getattr(plt, 'show', None)
    def _auto_show(*a, **k):
        try:
            ts = int(time.time()); fn = f"figure-{ts}.png"; plt.gcf(); plt.savefig(os.path.join(OUTPUT_DIR, fn))
        except Exception: pass
        if callable(_orig_show):
            try: _orig_show(*a, **k)
            except Exception: pass
    try: plt.show = _auto_show
    except Exception: pass
    if hasattr(plt, 'savefig'):
        _orig_savefig = plt.savefig
        def _savefig(fn, *a, **k):
            try:
                if not os.path.isabs(fn): fn = os.path.join(OUTPUT_DIR, fn)
            except Exception: pass
            return _orig_savefig(fn, *a, **k)
        try: plt.savefig = _savefig
        except Exception: pass
    try:
        _orig_anim_save = _anim_mod.Animation.save
        def _anim_save(self, filename, *args, **kwargs):
            try:
                if not os.path.isabs(filename): filename = os.path.join(OUTPUT_DIR, filename)
            except Exception: pass
            return _orig_anim_save(self, filename, *args, **kwargs)
        _anim_mod.Animation.save = _anim_save
    except Exception: pass
except Exception: pass

runpy.run_path(sys.argv[1], run_name='__main__')
"""
                with open(wrapper_path, 'w', encoding='utf-8') as f:
                    f.write(code)

            def _arrancar_execucao(codigo: str):
                cid = ui.context.client.id
                st = ensure_state(cid)
                out = st['output_widget']
                out.value = ''; out.update()
                if st['proc'] and st['proc'].poll() is None:
                    ui.notify('Já existe um processo a correr. Pára primeiro.', color='warning'); return

                script_name = os.path.join(TMP_DIR, f'{uuid.uuid4().hex}.py')
                runner_name = os.path.join(TMP_DIR, f'runner_{uuid.uuid4().hex}.py')
                st['script_path'] = script_name
                st['runner_path'] = runner_name
                try:
                    with open(script_name, 'w', encoding='utf-8') as f: f.write(codigo or '')
                    _write_runner(runner_name)
                    debug_log(f'Wrote script: {script_name} ({len(codigo.encode("utf-8"))} bytes)')
                except Exception as e:
                    out.value = f'Erro ao escrever ficheiros: {e}'; out.update(); return

                env = dict(os.environ)
                env['PYTHONUNBUFFERED']='1'; env['PYTHONIOENCODING']='utf-8'; env['MPLBACKEND']='Agg'; env['EXECUTOR_OUTPUT_DIR']=OUTPUT_DIR

                try:
                    master_fd, slave_fd = pty.openpty()
                    st['pty_master_fd'] = master_fd; st['use_pty']=True
                    st['proc'] = subprocess.Popen(
                        ['python3','-u', runner_name, script_name],
                        cwd=TMP_DIR, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                        text=False, bufsize=0, env=env, close_fds=True,
                    )
                    os.close(slave_fd)
                    debug_log(f'Spawned with PTY, pid={st["proc"].pid}')
                except Exception as e:
                    debug_log(f'PTY failed ({e}); falling back to pipes')
                    st['use_pty']=False
                    st['proc'] = subprocess.Popen(
                        ['python3','-u', runner_name, script_name],
                        cwd=TMP_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, bufsize=1, env=env,
                    )

                st['timeout_deadline'] = time.monotonic() + PY_TIMEOUT
                st['stopped'] = False
                st['start_time'] = time.time()
                status.text = f'Script a correr (PID {st["proc"].pid})'; ui.notify('Script iniciado', color='info')
                _start_stream_timer(cid)

            def executar_python():
                cid = ui.context.client.id
                CODE_STORE.pop(cid, None)
                ui.run_javascript(js_send_code(cid))
                attempts = {'n':0}
                def poll():
                    attempts['n']+=1
                    code = CODE_STORE.pop(cid, None)
                    if code is not None:
                        _arrancar_execucao(code); waiter.cancel()
                    elif attempts['n']>=100:
                        _arrancar_execucao(''); waiter.cancel()
                waiter = ui.timer(0.05, poll)

            def abrir_dialog_guardar():
                try:
                    save_name.value = f'script-{int(time.time())}.py'; save_name.update()
                except Exception: pass
                save_dialog.open()

            def parar_execucao():
                st = ensure_state(ui.context.client.id)
                if st['proc'] and st['proc'].poll() is None:
                    st['stopped'] = True
                    try: st['proc'].terminate()
                    except Exception: pass
                    ui.timer(0.3, once=True, callback=lambda: _finalizar_execucao(ui.context.client.id, st['proc'].returncode if st['proc'] else None))
                else:
                    ui.notify('Nenhum processo ativo', color='info')

            with ui.row().classes('gap-2 q-mt-xs'):
                ui.button('Executar Python', on_click=executar_python, color='green')
                ui.button('Parar Python',   on_click=parar_execucao, color='red')
                ui.button('Guardar .py',    on_click=abrir_dialog_guardar, color='primary')

            def inserir_snippet_midi():
                ui.run_javascript(r"""
                  if (window._cm_editor) {
                    window._cm_editor.setValue(
`# -*- coding: utf-8 -*-
# Exemplo MIDI mínimo com mido (pip install mido)
import os
from mido import Message, MidiFile, MidiTrack
mid = MidiFile()
track = MidiTrack(); mid.tracks.append(track)
track.append(Message('program_change', program=12, time=0))
for note in [60, 62, 64, 65, 67, 69, 71, 72]:
    track.append(Message('note_on', note=note, velocity=64, time=120))
    track.append(Message('note_off', note=note, velocity=64, time=120))
out = os.environ.get('EXECUTOR_OUTPUT_DIR','.')
mid.save(os.path.join(out, 'escala.mid'))
print('MIDI escrito em', os.path.join(out, 'escala.mid'))
`);
                  }
                """)
            ui.button('Inserir snippet MIDI', on_click=inserir_snippet_midi)

            def inserir_snippet_zip():
                ui.run_javascript(r"""
                  if (window._cm_editor) {
                    window._cm_editor.setValue(
`# -*- coding: utf-8 -*-
# Cria um ficheiro ZIP no OUTPUT_DIR
import os, zipfile, time
out = os.environ.get('EXECUTOR_OUTPUT_DIR','.')
zip_path = os.path.join(out, f'artefactos-{int(time.time())}.zip')
with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
    z.writestr('leia-me.txt', 'Gerado pelo executor às ' + time.strftime('%Y-%m-%d %H:%M:%S'))
print('ZIP criado em', zip_path)
`);
                  }
                """)
            ui.button('Inserir snippet ZIP', on_click=inserir_snippet_zip)

        # ---------- TAB SHELL ----------
        with ui.tab_panel(tab_shell):
            ui.label('Executar comandos de shell (ex.: pip install mido)').classes('text-md')
            cmd_box = ui.input(placeholder='Comando shell').props('outlined dense').style('width:100%;')
            saida_sh = ui.textarea(placeholder='Saída do comando (Shell)').props('readonly outlined dense').style('width:100%;height:200px;margin-top:6px;')
            def run_shell():
                if REQUIRE_LOGIN and not session_ok():
                    ui.notify('Sessão expirada. Faz login novamente.', color='warning'); ui.navigate.to('/login'); return
                cmd = (cmd_box.value or '').strip()
                if not cmd:
                    ui.notify('Escreve um comando', color='warning'); return
                try:
                    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, cwd=OUTPUT_DIR, timeout=SH_TIMEOUT)
                    text = (r.stdout or '') + (('\n'+r.stderr) if r.stderr else '')
                    saida_sh.value = text; saida_sh.update()
                    if r.returncode == 0:
                        status.text = 'Comando shell executado'; ui.notify('Comando concluído', color='positive'); popup_msg.text='Comando shell executado com sucesso'
                    else:
                        status.text = f'Shell terminou com código {r.returncode}'; ui.notify('Avisos/erros', color='warning'); popup_msg.text=f'Comando shell terminou com erros (código {r.returncode})'
                    popup.open()
                except subprocess.TimeoutExpired:
                    saida_sh.value = 'Tempo limite excedido.'; status.text='Timeout no comando shell'; ui.notify('Timeout', color='negative'); popup_msg.text='Tempo limite no comando shell'; popup.open()
                except Exception as e:
                    saida_sh.value = f'Erro: {e}'; status.text='Erro ao executar shell'; ui.notify('Erro', color='negative'); popup_msg.text=f'Erro ao executar shell: {e}'; popup.open()
            with ui.row().classes('gap-2 q-mt-sm'):
                ui.button('Executar', on_click=run_shell, color='primary')
                ui.button('Limpar', on_click=lambda: (setattr(saida_sh,'value',''), saida_sh.update()))

    # ---------- LISTA DE FICHEIROS & PREVIEW ----------
    with ui.row().classes('items-center justify-between w-full'):
        ui.label('Ficheiros disponíveis').classes('text-md')
        btn_refresh = ui.button('Atualizar lista', color='grey')
    files_list = ui.column().classes('w-full')

    def preview_file(fn: str):
        preview_area.clear()
        preview_title.text = f'Pré-visualização — {fn}'
        base_url = f'/download/{fn}'
        _, ext = os.path.splitext(fn); ext = ext.lower()
        with preview_area:
            if ext in PREVIEW_IMG_EXT:
                ui.image(base_url).classes('preview-media')
            elif ext in PREVIEW_VIDEO_EXT:
                ui.html(f'<video class="preview-media" controls src="{base_url}"></video>')
            elif ext in PREVIEW_HTML_EXT:
                ui.html(f'<iframe class="preview-iframe" src="{base_url}"></iframe>')
            elif ext in PREVIEW_AUDIO_EXT:
                ui.html(f'<audio class="preview-media" controls playsinline src="{base_url}"></audio>')
            elif ext in PREVIEW_MIDI_EXT:
                audio_id = f'audio_{uuid.uuid4().hex}'
                ui.html(f'<audio id="{audio_id}" class="preview-media" controls playsinline></audio>')
                log_label = ui.label('').classes('text-xs text-gray-600')
                def _render_and_play():
                    mid_path = os.path.join(OUTPUT_DIR, fn)
                    ok, url_rel, fmt, log = _render_midi(mid_path)
                    if not ok:
                        ui.notify('Falha a renderizar (ver log)', color='negative')
                        try:
                            log_label.text = (log or '')[:800]
                            log_label.update()
                        except Exception:
                            pass
                        return
                    js = f"const a=document.getElementById('{audio_id}'); a.src={json.dumps(url_rel)}; a.play().catch(()=>{{}});"
                    ui.run_javascript(js)
                    ui.notify(f'Render OK ({fmt})', color='positive')
                    try:
                        log_label.text = 'Render OK'
                        log_label.update()
                    except Exception:
                        pass
                with ui.row().classes('items-center gap-2'):
                    ui.button('Renderizar & Tocar', on_click=_render_and_play, color='green')
                    ui.button('Download MIDI', on_click=lambda filename=fn: ui.navigate.to(f'/download/{filename}')).props('flat')
            elif ext == '.php':
                path = os.path.join(OUTPUT_DIR, fn)
                try:
                    content = open(path,'r',encoding='utf-8',errors='replace').read()
                except Exception as e:
                    ui.label(f'Erro a ler ficheiro: {e}')
                else:
                    ui.code(content, language='php')
                with ui.row().classes('gap-2 q-mt-sm'):
                    ui.button('Download PHP', on_click=lambda filename=fn: ui.navigate.to(f'/download/{filename}')).props('flat')
            elif ext in PREVIEW_CODE_EXT:
                path = os.path.join(OUTPUT_DIR, fn)
                try:
                    content = open(path,'r',encoding='utf-8',errors='replace').read()
                except Exception as e:
                    ui.label(f'Erro a ler ficheiro: {e}')
                else:
                    lang = 'text'
                    if ext == '.js': lang='javascript'
                    elif ext == '.java': lang='java'
                    elif ext == '.php': lang='php'
                    try: ui.code(content, language=lang)
                    except Exception: ui.textarea(value=content).props('readonly outlined dense').style('width:100%;height:74vh;')
            else:
                ui.label('Sem pré-visualização para este tipo de ficheiro.')
        preview_dialog.open()

    def atualizar_lista():
        files_list.clear()
        ficheiros = [f for f in sorted(os.listdir(OUTPUT_DIR)) if f not in ('.tmp',) and not f.startswith('.')]
        if not ficheiros:
            with files_list: ui.label('Ainda não há ficheiros. Executa algo acima.'); return

        def _confirm_delete(filename: str):
            js = """
            (async function(){
              const ok = window.confirm('Eliminar "__FILENAME__"? Esta ação é irreversível.');
              if(!ok) return;
              const r = await fetch('/__delete__', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({'filename': "__FILENAME__"})
              });
              const j = await r.json().catch(()=>({'ok':false}));
              if (j.ok){
                if (navigator.vibrate) navigator.vibrate(40);
              } else {
                alert('Erro a eliminar: ' + (j.error||'desconhecido'));
              }
            })();
            """.replace('__FILENAME__', filename)
            ui.run_javascript(js)
            ui.timer(0.45, once=True, callback=atualizar_lista)

        with files_list:
            for f in ficheiros:
                _, ext = os.path.splitext(f); ext = ext.lower()
                with ui.row().classes('file-row w-full q-mb-xs touch-pan'):
                    ui.label(f).classes('file-name text-sm')
                    with ui.row().classes('file-actions'):
                        if (ext in PREVIEW_IMG_EXT) or (ext in PREVIEW_HTML_EXT) or (ext in PREVIEW_VIDEO_EXT) or (ext in PREVIEW_MIDI_EXT) or (ext in PREVIEW_AUDIO_EXT) or (ext in PREVIEW_CODE_EXT):
                            ui.button('Pré-visualizar', on_click=lambda fn=f: preview_file(fn)).props('flat size=sm')
                        dl_url = build_download_url(f)
                        ui.button('Download', on_click=lambda url=dl_url: ui.navigate.to(url)).props('flat size=sm')
                        ui.button('Eliminar', on_click=lambda fn=f: _confirm_delete(fn)).props('flat size=sm color=negative')

    btn_refresh.on('click', lambda _: atualizar_lista())
    atualizar_lista()

# ==========================
# RUN APP
# ==========================
ui.run(port=PORT, title='Executor NiceGUI', reload=False, storage_secret=SESSION_SECRET)
