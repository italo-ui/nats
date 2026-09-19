import os, time, base64, json, subprocess, urllib.request, urllib.error, re
import io
import hmac, functools
try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 (nome novo)
except Exception:
    try:
        import fitz  # alias legado, ainda aceito
    except Exception:
        fitz = None
try:
    import pytesseract
    from PIL import Image
except Exception:
    pytesseract = None
    Image = None
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright
app = Flask(__name__)
import threading, gc
_lock_navegador = threading.Lock()
# LOCK DE OCR (16/09/2026). O _lock_navegador so cobre a navegacao; o download e o OCR do
# /processar rodavam FORA dele, e duas chamadas simultaneas (duas rodadas do NATJUS2) faziam
# dois OCRs a 300 dpi ao mesmo tempo, ao lado de um Chromium — foi o que derrubou o servico por
# falta de memoria em 16/09. Este lock serializa a fase de download+OCR entre chamadas do
# /processar, sem segurar o navegador (um /nt do historico pode rodar enquanto o OCR trabalha).
_lock_ocr = threading.Lock()

# ============================================================
# AUTENTICACAO DAS ROTAS (18/09/2026, revisao)
# ------------------------------------------------------------
# Ate aqui todas as rotas eram publicas, inclusive /preencher (a unica que ESCREVE no
# e-NatJus), /login e /processar (OCR caro). Quem descobrisse a URL preenchia NTs ou
# derrubava o servico.
#   NAT_API_KEY (Railway > Variables): se definida, toda rota exceto "/" exige o header
#       X-Nat-Key: <valor>        (ou Authorization: Bearer <valor>)
#   Se NAO definida, o servico continua aberto como antes (compatibilidade com o n8n ate a
#   credencial existir la); a resposta de "/" avisa que a chave esta desligada.
# No n8n: credencial "Header Auth" (Name = X-Nat-Key, Value = a chave) em cada no HTTP que
# chama o Railway — mesmo modelo ja usado para a chave do Gemini.
# ============================================================
NAT_API_KEY = os.environ.get("NAT_API_KEY", "").strip()


@app.before_request
def _exigir_chave():
    if request.path == "/" or request.method == "OPTIONS":
        return None
    if not NAT_API_KEY:
        return None
    enviada = (request.headers.get("X-Nat-Key") or "").strip()
    if not enviada:
        auth = request.headers.get("Authorization", "") or ""
        if auth.lower().startswith("bearer "):
            enviada = auth[7:].strip()
    if not enviada or not hmac.compare_digest(enviada, NAT_API_KEY):
        return jsonify({"erro": "nao autorizado: header X-Nat-Key ausente ou invalido"}), 401
    return None


def _serializado(fn):
    """Roda a rota inteira sob _lock_navegador: UM Chromium por vez em todo o servico.

    (18/09/2026) /preencher, /campos, /diagnostico, /login e /teste abriam o navegador fora
    da trava — a regra de sessao unica valia so para /processar, /listar, /conclusao e /nt.
    Dois Chromium ao lado de um OCR foi o que derrubou o servico por memoria em 16/09.
    """
    @functools.wraps(fn)
    def _envolvida(*args, **kwargs):
        with _lock_navegador:
            return fn(*args, **kwargs)
    return _envolvida


class ServicoOcupado(Exception):
    pass


def _adquirir(lock, t_inicio, orcamento, nome):
    """(19/09/2026) Espera pelo lock so enquanto sobrar orcamento. Antes, uma chamada que o n8n
    ja tinha abandonado por timeout continuava rodando aqui e segurava _lock_ocr por minutos;
    as chamadas seguintes esperavam, gastavam o orcamento na fila e devolviam 'nenhum anexo
    baixado' — que a triagem lia como ILEGIVEL, um estado terminal. Agora a espera tem prazo e,
    esgotado, a resposta e 503 'servico ocupado', que o n8n trata como falha transitoria."""
    restante = orcamento - (time.time() - t_inicio)
    if restante <= 15 or not lock.acquire(timeout=max(1, restante - 15)):
        raise ServicoOcupado(f"servico ocupado com outra NT ({nome}); orcamento de {orcamento}s esgotado na espera")


ENATJUS_BASE = "https://www.pje.jus.br/e-natjus"
LOGIN_URL     = f"{ENATJUS_BASE}/index.php"
LISTA_URL     = f"{ENATJUS_BASE}/notaTecnica-solicitacao-listar.php"
DADOS_URL     = f"{ENATJUS_BASE}/notaTecnica-dados.php?idNotaTecnica={{nt}}"
DOWNLOAD_URL  = f"{ENATJUS_BASE}/arquivo-download.php?hash={{hash}}"
COOKIES_JSON_ENV = "ENATJUS_COOKIES"

# ============================================================
# LOGIN AUTOMATICO — fim da renovacao manual de cookies
# ------------------------------------------------------------
# Variaveis de ambiente (Railway > Variables):
#   ENATJUS_USER        login do e-NatJus
#   ENATJUS_PASS        senha do e-NatJus
#   COOKIE_STORE_PATH   opcional, ex.: /data/enatjus_cookies.json (Volume) -> mantem sessao entre deploys
#   ENATJUS_SESSION_TTL opcional, segundos (default 1500 = 25 min)
#   ENATJUS_COOKIES     opcional (modo legado): se NAO houver USER/PASS, usa estes cookies
# Se ENATJUS_USER/ENATJUS_PASS estiverem definidos, o servico loga sozinho e
# reautentica automaticamente quando a sessao expira. Caso contrario, cai no
# modo legado (cookies do ENATJUS_COOKIES), preservando o comportamento antigo.
# ============================================================
ENATJUS_USER = os.environ.get("ENATJUS_USER", "")
ENATJUS_PASS = os.environ.get("ENATJUS_PASS", "")
COOKIE_STORE_PATH = os.environ.get("COOKIE_STORE_PATH", "")
SESSION_TTL = int(os.environ.get("ENATJUS_SESSION_TTL", "1500"))

_cookies_cache = None      # lista no formato context.cookies() do Playwright
_cookies_ts = 0.0
_cookies_lock = threading.Lock()


def _persist_cookies(cookies):
    if not COOKIE_STORE_PATH:
        return
    try:
        with open(COOKIE_STORE_PATH, "w") as f:
            json.dump({"ts": time.time(), "cookies": cookies}, f)
    except Exception:
        pass


def _load_cookies_disk():
    if not COOKIE_STORE_PATH or not os.path.exists(COOKIE_STORE_PATH):
        return None
    try:
        with open(COOKIE_STORE_PATH) as f:
            data = json.load(f)
        if time.time() - float(data.get("ts", 0)) < SESSION_TTL:
            return data.get("cookies")
    except Exception:
        pass
    return None


def _cookies_env_seed():
    """Modo legado: converte ENATJUS_COOKIES (JSON do navegador) para o formato Playwright."""
    raw = os.environ.get(COOKIES_JSON_ENV, "")
    if not raw:
        return None
    try:
        cookies = json.loads(raw)
    except Exception:
        return None
    out = []
    for c in cookies:
        pc = {
            "name":   c.get("name", ""),
            "value":  c.get("value", ""),
            "domain": c.get("domain", "www.pje.jus.br"),
            "path":   c.get("path", "/"),
        }
        if "secure" in c:   pc["secure"]   = bool(c["secure"])
        if "httpOnly" in c: pc["httpOnly"] = bool(c["httpOnly"])
        ss = c.get("sameSite")
        if isinstance(ss, str) and ss in ("Strict", "Lax", "None"):
            pc["sameSite"] = ss
        if "expirationDate" in c:
            pc["expires"] = float(c["expirationDate"])
        out.append(pc)
    return out or None


def _get_cached_cookies():
    global _cookies_cache, _cookies_ts
    with _cookies_lock:
        if _cookies_cache and (time.time() - _cookies_ts) < SESSION_TTL:
            return _cookies_cache
        disk = _load_cookies_disk()
        if disk:
            _cookies_cache, _cookies_ts = disk, time.time()
            return _cookies_cache
        return None


def _set_cached_cookies(cookies):
    global _cookies_cache, _cookies_ts
    if not cookies:
        return
    with _cookies_lock:
        _cookies_cache, _cookies_ts = cookies, time.time()
    _persist_cookies(cookies)


def _renovar_sessao(context):
    """
    TTL DESLIZANTE (15/09/2026). Chamado depois de toda navegacao que confirmou
    'logado'. Antes, o carimbo do cache so era renovado no login: com o TTL fixo
    de 25 min, um lote de 3h40 (levantamento do historico) refazia login ~9 vezes
    por noite. Agora, enquanto o portal continua aceitando a sessao, o cache e
    renovado a cada uso e o login acontece UMA vez por lote. Se o portal derrubar
    a sessao, a autocura de _navegar_direto/_navegar_via_listagem/listar reloga
    uma vez, como antes.
    Regrava tambem os cookies atuais do contexto: o portal pode rotacionar o id
    de sessao, e manter a versao antiga levaria a um relogin desnecessario.
    """
    try:
        _set_cached_cookies(context.cookies())
    except Exception:
        pass


def _invalidate_cookies():
    global _cookies_cache, _cookies_ts
    with _cookies_lock:
        _cookies_cache, _cookies_ts = None, 0.0


def _tem_credenciais():
    return bool(ENATJUS_USER and ENATJUS_PASS)


def _login_no_contexto(context, page):
    """Faz login no e-NatJus DENTRO do contexto/navegador atual (sem abrir um segundo browser)."""
    if not _tem_credenciais():
        raise Exception("Login automatico indisponivel: defina ENATJUS_USER e ENATJUS_PASS")
    page.goto(LOGIN_URL, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)
    if _check_logged_in(page):
        _set_cached_cookies(context.cookies())
        return
    # Seletores confirmados em https://www.pje.jus.br/e-natjus/ (form id="formLogin").
    # O token CSRF (input hidden name="token") ja vem preenchido pela pagina; nao mexer.
    page.fill("#login", ENATJUS_USER)
    page.fill("#senha", ENATJUS_PASS)
    try:
        page.click("#formLogin button[type=submit]", timeout=15000)  # botao "Entrar" -> login() via AJAX
    except Exception:
        try:
            page.evaluate("typeof login === 'function' && login()")
        except Exception:
            pass
    page.wait_for_load_state("networkidle", timeout=30000)
    if not _check_logged_in(page):
        time.sleep(2)
        try:
            page.goto(LISTA_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
    if not _check_logged_in(page):
        raise Exception("Login automatico falhou (credenciais invalidas ou layout do e-NatJus mudou)")
    _set_cached_cookies(context.cookies())


def _exigir_sessao(context, page, forcar=False):
    """
    Garante uma sessao valida no contexto:
      - injeta cookies em cache (rapido) quando possivel;
      - senao, faz login automatico (se houver credenciais);
      - modo legado: usa ENATJUS_COOKIES quando nao ha credenciais.
    """
    if not forcar:
        cookies = _get_cached_cookies()
        if not cookies and not _tem_credenciais():
            cookies = _cookies_env_seed()
        if cookies:
            try:
                context.add_cookies(cookies)
                return
            except Exception:
                pass
    if _tem_credenciais():
        _login_no_contexto(context, page)
    else:
        seed = _cookies_env_seed()
        if seed:
            context.add_cookies(seed)
        else:
            raise Exception("Sem sessao: configure ENATJUS_USER/ENATJUS_PASS (login automatico) ou ENATJUS_COOKIES")


def _relogar_se_possivel(context, page):
    """Invalida o cache e refaz login no contexto atual. Retorna True se relogou."""
    if _tem_credenciais():
        _invalidate_cookies()
        _login_no_contexto(context, page)
        return True
    return False


def _launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--single-process", "--no-zygote", "--disable-setuid-sandbox",
            "--disable-extensions", "--memory-pressure-off"
        ]
    )
def _check_logged_in(page):
    try:
        return "Login" not in page.inner_text("nav")
    except Exception:
        return False
def _screenshot_b64(page):
    try:
        return base64.b64encode(page.screenshot(full_page=True)).decode()
    except Exception:
        return None
def _cookie_str(context):
    return "; ".join(f"{c['name']}={c['value']}" for c in context.cookies())
def _aguardar_conteudo(page):
    for seletor in [
        "input.fileform",
        "text=Baixar arquivo",
        ".dm-uploader",
        "#conteudo form",
        "#conteudo table",
    ]:
        try:
            page.wait_for_selector(seletor, timeout=15000)
            return
        except Exception:
            pass
    time.sleep(5)
def _navegar_direto(context, page, nt):
    url = DADOS_URL.format(nt=nt)
    page.goto(url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)
    if not _check_logged_in(page):
        # autocura: reloga uma vez e refaz a navegacao
        if _relogar_se_possivel(context, page):
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
        if not _check_logged_in(page):
            raise Exception("Sessão expirada ou cookies inválidos")
    _renovar_sessao(context)  # TTL deslizante: sessao confirmada, renova o cache
    if "notaTecnica-solicitacao-listar" in page.url:
        raise Exception(f"NT {nt} não encontrada — redirecionado para listagem")
    _aguardar_conteudo(page)
    return page
def _navegar_via_listagem(context, page, nt):
    page.goto(LISTA_URL, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)
    if not _check_logged_in(page):
        # autocura: reloga uma vez e refaz a navegacao
        if _relogar_se_possivel(context, page):
            page.goto(LISTA_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
        if not _check_logged_in(page):
            raise Exception("Sessão expirada ou cookies inválidos")
    _renovar_sessao(context)  # TTL deslizante
    linha = page.locator(f"tr:has-text('{nt}')").first
    if linha.count() == 0:
        raise Exception(f"NT {nt} não encontrada na listagem")
    btn_acoes = linha.locator("button, a").filter(has_text="Ações").first
    if btn_acoes.count() == 0:
        btn_acoes = linha.locator(".dropdown-toggle, [data-toggle='dropdown']").first
    btn_acoes.click()
    time.sleep(1)
    opcao_nt = page.locator("a:has-text('Nota Técnica'), a:has-text('Nota Tecnica')").first
    with context.expect_page() as nova_aba_info:
        opcao_nt.click()
    pagina_nt = nova_aba_info.value
    pagina_nt.wait_for_load_state("networkidle", timeout=30000)
    _aguardar_conteudo(pagina_nt)
    return pagina_nt
def _navegar_ate_pagina_nt(context, page, nt):
    try:
        return _navegar_direto(context, page, nt)
    except Exception as e1:
        try:
            page2 = context.new_page()
            return _navegar_via_listagem(context, page2, nt)
        except Exception as e2:
            raise Exception(f"Falha nas duas estratégias. Direto: {e1} | Listagem: {e2}")
def _extrair_hashes(pagina_nt):
    """Le os hashes dos arquivos da NT.
    Metodo principal: links visiveis de download (<a href="arquivo-download.php?hash=...">).
    Fallback: inputs escondidos do formulario (input.fileform).
    """
    hashes = []
    try:
        # 1) links de download visiveis (pega TODOS os arquivos da NT)
        try:
            links = pagina_nt.locator("a[href*='arquivo-download.php?hash=']")
            for i in range(links.count()):
                try:
                    href = links.nth(i).get_attribute("href") or ""
                    m = re.search(r"hash=([A-Za-z0-9]+)", href)
                    if m:
                        h = m.group(1).strip()
                        if h and not any(x.get("hash") == h for x in hashes):
                            hashes.append({"hash": h, "nome": ""})
                except Exception:
                    pass
        except Exception:
            pass
        # 2) fallback: inputs escondidos do formulario
        if not hashes:
            inputs = pagina_nt.locator("input.fileform")
            nomes  = pagina_nt.locator("input.filename")
            for i in range(inputs.count()):
                try:
                    hash_val = inputs.nth(i).get_attribute("value") or ""
                    nome_val = ""
                    try:
                        nome_val = nomes.nth(i).get_attribute("value") or ""
                    except Exception:
                        pass
                    if hash_val.strip():
                        hashes.append({"hash": hash_val.strip(), "nome": nome_val.strip()})
                except Exception:
                    pass
    except Exception as e:
        hashes.append({"erro": str(e)})
    return hashes
# 18/09/2026: teto por anexo. A NT 576675 (autos federais grandes) derrubou o Railway por
# memoria (502). Acima do teto o arquivo nao e nem baixado inteiro: a leitura e interrompida e
# o anexo entra no texto como ignorado, para a triagem manual decidir.
MAX_ARQUIVO_MB = int(os.environ.get("MAX_ARQUIVO_MB", "60"))


class ArquivoGrandeDemais(Exception):
    pass


def _baixar_arquivo(hash_val, cookie_str, max_bytes=None):
    url = DOWNLOAD_URL.format(hash=hash_val)
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie_str)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    req.add_header("Referer", ENATJUS_BASE + "/")
    # 17/09/2026: 90 s por arquivo (era 60) — o n8n agora espera 4 min pela chamada inteira.
    with urllib.request.urlopen(req, timeout=90) as resp:
        content_type = resp.headers.get("Content-Type", "")
        if max_bytes:
            try:
                declarado = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                declarado = 0
            if declarado > max_bytes:
                raise ArquivoGrandeDemais(f"{declarado / 1048576:.0f} MB, acima do teto de {max_bytes // 1048576} MB")
            partes = []
            lidos = 0
            while True:
                bloco = resp.read(1048576)
                if not bloco:
                    break
                lidos += len(bloco)
                if lidos > max_bytes:
                    raise ArquivoGrandeDemais(f"mais de {max_bytes // 1048576} MB")
                partes.append(bloco)
            conteudo = b"".join(partes)
        else:
            conteudo = resp.read()
    return conteudo, content_type
def _selecionar_opcao(page, seletor, valor):
    """Seleciona uma opção em um <select> pelo valor ou texto visível."""
    try:
        page.select_option(seletor, value=valor)
        return True
    except Exception:
        try:
            page.select_option(seletor, label=valor)
            return True
        except Exception:
            return False
def _preencher_campo(page, seletor, valor):
    """Preenche um campo de texto, limpando antes."""
    try:
        page.fill(seletor, "")
        page.fill(seletor, valor)
        return True
    except Exception:
        return False
def _navegar_ate_formulario(context, page, nt):
    """
    Abre a pagina da NT. NAO existe botao 'Preencher' no e-NatJus: a pagina da NT JA E o
    formulario. A versao anterior desta funcao procurava a:has-text('Preencher') e variacoes,
    nao achava nenhuma e levantava excecao depois de ter chegado exatamente onde precisava.

    Confirmado na sondagem da NT 568896: a pagina traz os anexos, os dados do paciente, do
    advogado e do processo, e entao uma aba por tecnologia com o formulario inteiro dentro.
    """
    pagina = _navegar_ate_pagina_nt(context, page, nt)
    try:
        pagina.wait_for_selector("text=Diagnóstico Principal", timeout=20000)
    except Exception:
        raise Exception(
            "A pagina da NT abriu, mas a aba da tecnologia nao apareceu. "
            "Verifique se a NT tem uma tecnologia cadastrada (link '+ Adicionar Tecnologia')."
        )
    return pagina


def _grupo_do_rotulo(pagina, texto):
    """
    Devolve o container do campo a partir do texto do rotulo.

    O CASAMENTO E ANCORADO, e isso nao e preciosismo. No teste de 15/09/2026 o padrao era
    substring e o rotulo 'CID' casou com 'Cidade' — que existe na mesma pagina, nos dados do
    paciente, e aparece ANTES. O .first pegou o campo errado. Uma busca frouxa nao falha: ela
    acerta outra coisa, que e o modo de errar mais caro que existe num formulario.

    Tenta o padrao exato (aceitando o asterisco de obrigatorio no fim); so cai para substring
    se o exato nao achar nada, porque alguns rotulos sao longos e quebram em varias linhas.
    """
    exato = re.compile(r"^\s*" + re.escape(texto) + r"\s*\*?\s*$", re.IGNORECASE)
    lab = pagina.locator("label").filter(has_text=exato)
    if lab.count() == 0:
        lab = pagina.locator("label").filter(has_text=texto)
    if lab.count() == 0:
        raise Exception("rotulo nao encontrado: " + texto)
    primeiro = lab.first
    primeiro.wait_for(state="attached", timeout=8000)
    return primeiro.locator("xpath=ancestor::div[contains(@class,'form-group')][1]")


def _texto_por_rotulo(pagina, rotulo, valor, log, nome):
    """Preenche input ou textarea localizado pelo rotulo."""
    try:
        grupo = _grupo_do_rotulo(pagina, rotulo)
        campo = grupo.locator("input[type='text']:visible, input:not([type]):visible, textarea:visible").first
        campo.wait_for(state="visible", timeout=8000)
        campo.fill("")
        campo.fill(str(valor))
        lido = campo.input_value()
        ok = lido.strip() == str(valor).strip()
        log.append(f"{nome}: {'OK' if ok else 'GRAVOU DIFERENTE (' + lido[:40] + ')'}")
        return ok
    except Exception as e:
        log.append(f"{nome}: FALHOU ({type(e).__name__}: {str(e)[:90]})")
        return False


def _select_por_rotulo(pagina, rotulo, valor, log, nome):
    """
    Seleciona uma opcao. Cobre os dois tipos de controle do formulario:

      1. <select> comum  -> select_option pelo rotulo visivel.
      2. combobox select2 (CID e NatJus Responsavel) -> o <select> original fica ESCONDIDO e
         quem aparece e o widget. No teste de 15/09/2026 o seletor do gatilho incluia
         '.form-control', que casou justamente com o select escondido; o .first pegou ele e o
         click ficou esperando um elemento invisivel ate estourar o timeout. Por isso agora
         todo seletor de gatilho traz ':visible' e o '.form-control' saiu da lista: ele e o
         que o widget substitui, nao o widget.

    A caixa de busca do select2 e anexada ao <body>, fora do form-group — por isso ela e
    procurada na pagina inteira, e nao dentro do grupo.
    """
    alvo = str(valor)
    try:
        grupo = _grupo_do_rotulo(pagina, rotulo)
    except Exception as e:
        log.append(f"{nome}: FALHOU ({str(e)[:90]})")
        return False

    # 1. <select> comum
    try:
        sel = grupo.locator("select:visible").first
        if sel.count():
            try:
                sel.select_option(label=alvo, timeout=5000)
            except Exception:
                sel.select_option(value=alvo, timeout=5000)
            log.append(f"{nome}: OK ({alvo})")
            return True
    except Exception:
        pass

    # 2. combobox select2
    try:
        gatilho = grupo.locator(
            ".select2-selection:visible, .select2-choice:visible, .select2-container:visible, "
            ".selectize-input:visible, [role='combobox']:visible"
        ).first
        gatilho.wait_for(state="visible", timeout=8000)
        gatilho.click()
        pagina.wait_for_timeout(500)
        busca = pagina.locator(
            "input.select2-search__field:visible, .select2-search input:visible, "
            ".selectize-input input:visible, input[role='searchbox']:visible"
        ).last
        try:
            busca.fill(alvo, timeout=4000)
        except Exception:
            pagina.keyboard.type(alvo, delay=40)
        pagina.wait_for_timeout(1200)
        opcao = pagina.locator(
            ".select2-results__option:visible, .select2-result-label:visible, "
            ".selectize-dropdown-content .option:visible, li[role='option']:visible"
        ).first
        opcao.wait_for(state="visible", timeout=8000)
        escolhida = (opcao.inner_text() or "").strip()
        # Uma lista que nao achou nada mostra 'Nenhum resultado' e clicar nisso nao seleciona.
        if re.search(r"nenhum resultado|no results|carregando|searching", escolhida, re.IGNORECASE):
            pagina.keyboard.press("Escape")
            log.append(f"{nome}: FALHOU (a busca por '{alvo}' nao devolveu opcao: '{escolhida[:40]}')")
            return False
        opcao.click()
        pagina.wait_for_timeout(400)
        log.append(f"{nome}: OK ({escolhida[:60]})")
        return True
    except Exception as e:
        log.append(f"{nome}: FALHOU ({type(e).__name__}: {str(e)[:90]})")
        return False

# ─────────────────────────────────────────────
# Rotas básicas
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "chave_api": bool(NAT_API_KEY), "versao": "2026-09-20b"}), 200
@app.route("/teste", methods=["GET"])
@_serializado
def teste():
    try:
        result = subprocess.run(
            ["python", "-c",
             "from playwright.sync_api import sync_playwright; "
             "p = sync_playwright().start(); "
             "b = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage','--single-process']); "
             "b.close(); p.stop(); print('OK')"],
            capture_output=True, text=True, timeout=60
        )
        # --- status do OCR: se qualquer peca faltar, o OCR morre em SILENCIO ---
        ocr = {
            "pymupdf": fitz is not None,
            "pytesseract": pytesseract is not None,
            "pillow": Image is not None,
            "pdfplumber": False,
            "tesseract_bin": None,
            "idiomas": [],
        }
        try:
            import pdfplumber  # noqa: F401
            ocr["pdfplumber"] = True
        except Exception:
            pass
        if pytesseract is not None:
            try:
                ocr["tesseract_bin"] = str(pytesseract.get_tesseract_version())
                ocr["idiomas"] = sorted(pytesseract.get_languages(config=""))
            except Exception as e:
                ocr["erro"] = str(e)
        ocr["ok"] = bool(
            ocr["pymupdf"] and ocr["pytesseract"] and ocr["pillow"]
            and ocr["pdfplumber"] and ocr["tesseract_bin"] and "por" in ocr["idiomas"]
        )
        return jsonify({
            "playwright": "OK" if result.returncode == 0 else "ERRO",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "ocr": ocr
        })
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
@app.route("/login", methods=["GET"])
@_serializado
def login():
    """Forca uma (re)autenticacao automatica e reporta o estado da sessao."""
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page    = context.new_page()
        try:
            _exigir_sessao(context, page, forcar=True)
            # confirma navegando para a listagem
            try:
                page.goto(LISTA_URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            autenticado = _check_logged_in(page)
            resultado = {
                "login_automatico": _tem_credenciais(),
                "autenticado": autenticado,
                "url_final": page.url,
                "cookies": len(context.cookies()),
                "screenshot": _screenshot_b64(page)
            }
            browser.close()
            return jsonify(resultado)
        except Exception as e:
            sc = _screenshot_b64(page)
            browser.close()
            return jsonify({"erro": str(e), "screenshot": sc}), 500
@app.route("/diagnostico/<nt>", methods=["GET"])
@_serializado
def diagnostico(nt):
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()
        try:
            _exigir_sessao(context, page)
            pagina_nt = _navegar_ate_pagina_nt(context, page, nt)
            hashes    = _extrair_hashes(pagina_nt)
            screenshot = _screenshot_b64(pagina_nt)
            browser.close()
            return jsonify({
                "nt": nt,
                "url_pagina": pagina_nt.url,
                "estrategia": "direta" if "notaTecnica-dados" in pagina_nt.url else "listagem",
                "hashes_encontrados": hashes,
                "screenshot": screenshot
            })
        except Exception as e:
            sc = _screenshot_b64(page)
            browser.close()
            return jsonify({"erro": str(e), "screenshot": sc}), 500
@app.route("/baixar", methods=["POST"])
def baixar():
    """(LEGADO) Baixa o PDF e devolve base64. Substituído por /processar."""
    nt = request.json.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400
    pdfs  = []
    erros = []
    with _lock_navegador:  # garante UM navegador por vez (evita estouro de memória)
      with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()
        try:
            _exigir_sessao(context, page)
            pagina_nt = _navegar_ate_pagina_nt(context, page, nt)
            hashes    = _extrair_hashes(pagina_nt)
            hashes_validos = [h for h in hashes if "hash" in h and h["hash"]]
            if not hashes_validos:
                sc = _screenshot_b64(pagina_nt)
                browser.close()
                return jsonify({
                    "erro": "Nenhum anexo com hash encontrado na NT",
                    "url_pagina": pagina_nt.url,
                    "screenshot": sc
                }), 404
            cookies = _cookie_str(context)
            browser.close()
            for idx, info in enumerate(hashes_validos[:5]):
                try:
                    conteudo, content_type = _baixar_arquivo(info["hash"], cookies)
                    nome = info.get("nome") or f"NT_{nt}_arquivo_{idx+1}.pdf"
                    pdfs.append({
                        "nome": nome,
                        "hash": info["hash"],
                        "content_type": content_type,
                        "base64": base64.b64encode(conteudo).decode()
                    })
                except Exception as e:
                    erros.append(f"Arquivo {idx+1} (hash={info['hash'][:12]}...): {str(e)}")
        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "avisos": erros}), 500
    if not pdfs:
        return jsonify({"erro": "Nenhum PDF baixado", "detalhes": erros}), 500
    return jsonify({
        "numeroNT": nt,
        "total_pdfs": len(pdfs),
        "pdfs": pdfs,
        "avisos": erros
    })
# ─────────────────────────────────────────────
# CACHE DA EXTRACAO DOS AUTOS
# ─────────────────────────────────────────────
# O texto dos autos e pedido DUAS vezes para as NTs distribuidas ao Italo: uma pelo
# NATJUS2 (triagem, 18h) e outra pelo Processamento (rascunho da NT, 5-6h do dia
# seguinte). Cada extracao faz login, baixa todos os anexos e roda OCR — trabalho caro
# e identico. Guardamos o resultado por NT para a segunda chamada sair de graca.
#
#   EXTRACAO_CACHE_DIR  onde gravar (default /tmp/nat_extracao; use um Volume do
#                       Railway se quiser que sobreviva a deploy)
#   EXTRACAO_TTL        validade em segundos (default 7 dias; 0 desliga o cache)
#
# Extracao ILEGIVEL nao entra no cache — senao um retry nunca reprocessaria.
EXTRACAO_CACHE_DIR = os.environ.get("EXTRACAO_CACHE_DIR", "/tmp/nat_extracao")
EXTRACAO_TTL = int(os.environ.get("EXTRACAO_TTL", str(7 * 24 * 3600)))

# 18/09/2026: ORCAMENTO TOTAL do /processar, contado desde a chegada da requisicao (login,
# navegacao, download e OCR), e nao so a partir do download. O n8n espera 240 s; com 170 s
# sobra folga para a resposta e para a espera pela trava do navegador. A NT 577659 (23 paginas
# de OCR) estourava 3 x 240 s porque o relogio so comecava depois da navegacao.
PROCESSAR_ORCAMENTO_S = int(os.environ.get("PROCESSAR_ORCAMENTO_S", "170"))

# 18/09/2026: chamada REPETIDA da mesma NT (retry do n8n enquanto a primeira ainda roda)
# espera a em curso terminar e devolve o cache, em vez de baixar e fazer OCR de novo.
_em_curso = {}
_em_curso_lock = threading.Lock()


def _cache_caminho(nt):
    nome = re.sub(r"[^0-9A-Za-z_-]", "", str(nt))
    return os.path.join(EXTRACAO_CACHE_DIR, nome + ".json")


def _cache_ler(nt):
    if EXTRACAO_TTL <= 0:
        return None
    try:
        caminho = _cache_caminho(nt)
        if not os.path.exists(caminho):
            return None
        with open(caminho) as f:
            d = json.load(f)
        idade = time.time() - float(d.get("ts", 0))
        if idade > EXTRACAO_TTL:
            return None
        payload = d.get("payload")
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["cache"] = "hit"
            payload["cache_idade_s"] = int(idade)
        return payload
    except Exception:
        return None


def _cache_gravar(nt, payload):
    if EXTRACAO_TTL <= 0:
        return
    if str((payload or {}).get("legibilidade", "")).lower() == "ilegivel":
        return  # deixa o retry tentar de novo
    try:
        os.makedirs(EXTRACAO_CACHE_DIR, exist_ok=True)
        with open(_cache_caminho(nt), "w") as f:
            json.dump({"ts": time.time(), "payload": payload}, f)
    except Exception:
        pass


def _texto_ruim(t):
    """True se o texto da pagina parece ilegivel: vazio, poucos chars ou lixo de fonte (cid)."""
    if not t or len(t.strip()) < 100:
        return True
    cid = t.count("(cid:")
    letras = sum(1 for c in t if c.isalpha())
    ratio = letras / max(len(t), 1)
    return cid > 20 or ratio < 0.5
def _otsu_threshold(hist):
    """Limiar de Otsu (binarizacao) a partir do histograma de cinza, em Python puro."""
    total = sum(hist)
    if total == 0:
        return 127
    soma_total = sum(i * hist[i] for i in range(256))
    soma_b = 0.0
    peso_b = 0
    max_var = -1.0
    limiar = 127
    for i in range(256):
        peso_b += hist[i]
        if peso_b == 0:
            continue
        peso_f = total - peso_b
        if peso_f == 0:
            break
        soma_b += i * hist[i]
        media_b = soma_b / peso_b
        media_f = (soma_total - soma_b) / peso_f
        var = peso_b * peso_f * (media_b - media_f) ** 2
        if var > max_var:
            max_var = var
            limiar = i
    return limiar
def _preprocess(img):
    """Cinza + binarizacao (Otsu): o Tesseract le muito melhor preto-no-branco."""
    g = img.convert("L")
    try:
        thr = _otsu_threshold(g.histogram()[:256])
        return g.point(lambda p: 255 if p > thr else 0)
    except Exception:
        return g
def _ocr_pagina(doc_fitz, indice, dpi=220):
    """Renderiza a pagina, pre-processa e roda OCR (portugues).

    MEMORIA (16/09/2026): antes renderizava em RGB a 300 dpi, codificava em PNG e decodificava
    de novo com PIL — tres copias de uma imagem de ~25 MB por pagina. Agora renderiza direto em
    CINZA a 220 dpi (o Tesseract le bem a partir de ~200) e monta a imagem PIL a partir dos
    bytes crus, sem PNG. Cerca de 6x menos memoria por pagina, e libera tudo ao sair.
    """
    if fitz is None or pytesseract is None or Image is None:
        return ""
    page = doc_fitz.load_page(indice)
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    try:
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    finally:
        pix = None
    try:
        img = _preprocess(img)
        return pytesseract.image_to_string(img, lang="por", config="--oem 1 --psm 6") or ""
    finally:
        try:
            img.close()
        except Exception:
            pass
        page = None
@app.route("/processar", methods=["POST"])
def processar():
    """
    [PRINCIPAL] Baixa o PDF da NT E extrai o texto, tudo internamente.
    Devolve SÓ o texto (leve) — o n8n nunca recebe o arquivo pesado.
    Mede a legibilidade (ok / parcial / ilegivel) para a triagem decidir.
    18/09/2026: orcamento total desde o inicio, espera pela chamada em curso da mesma NT,
    teto por anexo (MAX_ARQUIVO_MB) e aviso de anexos alem do 5o. O trabalho em si esta em
    _processar_nt(); esta funcao cuida do cache e da fila por NT.
    """
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        subprocess.run(["pip", "install", "pdfplumber", "-q"], check=True)
    nt = (request.json or {}).get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    t_inicio = time.time()  # 18/09/2026: o orcamento conta desde ja
    # ?forcar=1 (ou {"forcar": true}) ignora o cache e extrai de novo
    forcar = (str(request.args.get("forcar", "")) == "1") or bool(request.json.get("forcar"))
    if not forcar:
        em_cache = _cache_ler(nt)
        if em_cache:
            return jsonify(em_cache)

    # 18/09/2026: se esta NT ja esta sendo processada por outra chamada, espera e devolve o cache.
    with _em_curso_lock:
        evento_alheio = _em_curso.get(str(nt))
        if evento_alheio is None:
            evento_meu = threading.Event()
            _em_curso[str(nt)] = evento_meu
        else:
            evento_meu = None
    if evento_meu is None:
        evento_alheio.wait(timeout=max(10, PROCESSAR_ORCAMENTO_S + 40))
        em_cache = _cache_ler(nt)
        if em_cache:
            em_cache["cache"] = "hit (aguardou a chamada em curso)"
            return jsonify(em_cache)
        return jsonify({"erro": "extracao desta NT ainda em curso em outra chamada; tente de novo", "numeroNT": nt}), 503
    try:
        return _processar_nt(nt, t_inicio)
    finally:
        with _em_curso_lock:
            _em_curso.pop(str(nt), None)
        evento_meu.set()


def _extrair_paginas(conteudo, t_inicio, orcamento):
    """(19/09/2026) Texto nativo pagina a pagina com PyMuPDF — dezenas de vezes mais rapido e
    mais leve que o pdfplumber, que levava minutos num processo de 500 paginas e segurava a fila.
    O pdfplumber fica como reserva quando o PyMuPDF nao esta disponivel ou falha no arquivo.
    Respeita o orcamento: para de extrair quando o tempo acabar e devolve o que leu."""
    textos = []
    cortado = False
    if fitz is not None:
        doc = fitz.open(stream=conteudo, filetype="pdf")
        try:
            for i in range(doc.page_count):
                if (time.time() - t_inicio) >= orcamento:
                    cortado = True
                    break
                try:
                    textos.append(doc.load_page(i).get_text("text") or "")
                except Exception:
                    textos.append("")
        finally:
            doc.close()
        return textos, cortado
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
        for pg in pdf.pages:
            if (time.time() - t_inicio) >= orcamento:
                cortado = True
                break
            textos.append(pg.extract_text() or "")
            try:
                pg.flush_cache()
            except Exception:
                pass
    return textos, cortado


def _processar_nt(nt, t_inicio):
    try:
        _adquirir(_lock_navegador, t_inicio, PROCESSAR_ORCAMENTO_S, "navegador")
    except ServicoOcupado as e:
        return jsonify({"erro": str(e), "numeroNT": nt}), 503
    try:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context(accept_downloads=True)
            page    = context.new_page()
            try:
                _exigir_sessao(context, page)
                pagina_nt = _navegar_ate_pagina_nt(context, page, nt)
                hashes    = _extrair_hashes(pagina_nt)
                hashes_validos = [h for h in hashes if "hash" in h and h["hash"]]
                if not hashes_validos:
                    browser.close()
                    return jsonify({"erro": "Nenhum anexo encontrado na NT", "numeroNT": nt}), 404
                cookies = _cookie_str(context)
                browser.close()
            except Exception as e:
                try:
                    browser.close()
                except Exception:
                    pass
                return jsonify({"erro": str(e), "numeroNT": nt}), 500
    finally:
        _lock_navegador.release()
    # baixa todos os arquivos; usa texto nativo (PyMuPDF) onde der e faz OCR
    # SELETIVO so nas paginas escaneadas, com orcamento de tempo e de paginas.
    # Processa do menor para o maior (laudo/relatorio costuma ser o arquivo menor).
    texto_total = ""
    arquivos = 0
    paginas_total = 0
    paginas_ilegiveis = 0
    paginas_ocr = 0
    # 17/09/2026: janela do n8n subiu de 3 para 4 min (no 05 do NATJUS2 e 04 do Processamento).
    # 18/09/2026: o orcamento e TOTAL (PROCESSAR_ORCAMENTO_S, desde a chegada da requisicao) e
    # vale para download e OCR; t_inicio vem de processar().
    MAX_PAGINAS_OCR = 40      # teto de paginas que farao OCR
    OCR_TIME_BUDGET = PROCESSAR_ORCAMENTO_S
    avisos = []
    anexos_total = len(hashes_validos)
    if anexos_total > 5:
        avisos.append(f"A NT tem {anexos_total} anexos; so os 5 primeiros foram lidos.")
    # Download + OCR serializados entre chamadas do /processar (ver _lock_ocr no topo).
    try:
        _adquirir(_lock_ocr, t_inicio, PROCESSAR_ORCAMENTO_S, "download/OCR")
    except ServicoOcupado as e:
        return jsonify({"erro": str(e), "numeroNT": nt}), 503
    try:
      # 1) baixa o conteudo de cada arquivo
      baixados = []
      for idx, info in enumerate(hashes_validos[:5]):
        if (time.time() - t_inicio) >= OCR_TIME_BUDGET:
            avisos.append(f"Arquivo {idx+1} nao foi baixado: orcamento de tempo esgotado.")
            texto_total += f"\n[arquivo {idx+1} nao baixado: orcamento de tempo esgotado]\n"
            continue
        try:
            conteudo, _ct = _baixar_arquivo(info["hash"], cookies, max_bytes=MAX_ARQUIVO_MB * 1048576)
            baixados.append((idx, conteudo))
        except ArquivoGrandeDemais as e:
            avisos.append(f"Arquivo {idx+1} ignorado: {e}.")
            texto_total += f"\n[arquivo {idx+1} ignorado: {e} — leitura manual necessaria]\n"
        except Exception as e:
            texto_total += f"\n[erro ao baixar arquivo {idx+1}: {e}]\n"
      # 2) menor -> maior (gasta o orcamento de OCR nos arquivos menores primeiro)
      baixados.sort(key=lambda x: len(x[1]))
      # Consome a lista tirando cada arquivo dela: o conteudo ja processado e liberado
      # antes de abrir o proximo, em vez de ficar tudo em memoria ate o fim.
      while baixados:
        idx, conteudo = baixados.pop(0)
        try:
            textos, cortado = _extrair_paginas(conteudo, t_inicio, OCR_TIME_BUDGET)
            if not textos:
                # nenhuma pagina lida (orcamento ja esgotado ou PDF vazio): nao conta como arquivo lido
                avisos.append(f"Arquivo {idx+1} nao foi lido: orcamento de tempo esgotado antes da primeira pagina.")
                texto_total += f"\n[arquivo {idx+1} nao lido: orcamento de tempo esgotado]\n"
                continue
            if cortado:
                avisos.append(f"Arquivo {idx+1}: leitura interrompida na pagina {len(textos)} por orcamento de tempo.")
            doc_fitz = None
            for i, txt_pagina in enumerate(textos):
                paginas_total += 1
                if _texto_ruim(txt_pagina):
                    txt_ocr = ""
                    dentro_orcamento = (paginas_ocr < MAX_PAGINAS_OCR) and ((time.time() - t_inicio) < OCR_TIME_BUDGET)
                    if dentro_orcamento and fitz is not None:
                        try:
                            if doc_fitz is None:
                                doc_fitz = fitz.open(stream=conteudo, filetype="pdf")
                            txt_ocr = _ocr_pagina(doc_fitz, i)
                            paginas_ocr += 1
                        except Exception:
                            txt_ocr = ""
                    if _texto_ruim(txt_ocr):
                        paginas_ilegiveis += 1
                        texto_total += (txt_ocr or txt_pagina) + "\n"
                    else:
                        texto_total += txt_ocr + "\n"
                else:
                    texto_total += txt_pagina + "\n"
            if doc_fitz is not None:
                doc_fitz.close()
            texto_total += "\n--- fim do documento ---\n\n"
            arquivos += 1
        except Exception as e:
            texto_total += f"\n[erro ao ler arquivo: {e}]\n"
        finally:
            conteudo = None
            textos = None
            doc_fitz = None
            gc.collect()
    finally:
        _lock_ocr.release()
    texto_total = texto_total.strip()
    if arquivos == 0 and anexos_total > 0:
        # 19/09/2026: nenhum anexo lido (orcamento esgotado, download falhou ou arquivo acima do teto).
        # Devolver 200 com 'ilegivel' fazia a triagem marcar a NT como ILEGIVEL para sempre; 503 faz
        # o n8n tratar como transitorio (volta a 'novo'), e a triagem manual continua disponivel.
        return jsonify({"erro": "nenhum anexo foi baixado: " + (" ".join(avisos) or texto_total[:300]), "numeroNT": nt,
                        "anexos_total": anexos_total, "avisos": avisos}), 503
    # links diretos de download de cada arquivo no e-NatJus (sem trazer o PDF pro n8n)
    arquivos_links = [
        {
            "nome": (h.get("nome") or ("Arquivo " + str(i + 1))),
            "url": ENATJUS_BASE + "/arquivo-download.php?hash=" + h["hash"],
        }
        for i, h in enumerate(hashes_validos[:5])
    ]
    if paginas_total > 0:
        pct_ilegivel = round(100 * paginas_ilegiveis / paginas_total)
    else:
        pct_ilegivel = 100
    if pct_ilegivel >= 70:
        legibilidade = "ilegivel"
    elif pct_ilegivel >= 30:
        legibilidade = "parcial"
    else:
        legibilidade = "ok"
    if avisos:
        # a triagem (Claude) le o texto inteiro: o aviso no fim do texto vira pendencia na NT.
        texto_total += "\n\n[AVISO DO SISTEMA: " + " ".join(avisos) + "]\n"
    resultado = {
        "numeroNT": nt,
        "texto": texto_total,
        "caracteres": len(texto_total),
        "arquivos": arquivos,
        "anexos_total": anexos_total,
        "anexos_ignorados": max(0, anexos_total - arquivos),
        "avisos": avisos,
        "paginas_total": paginas_total,
        "paginas_ilegiveis": paginas_ilegiveis,
        "paginas_ocr": paginas_ocr,
        "pct_ilegivel": pct_ilegivel,
        "legibilidade": legibilidade,
        "tempo_s": round(time.time() - t_inicio, 1),
        "arquivos_links": arquivos_links,
        "cache": "miss"
    }
    _cache_gravar(nt, resultado)
    return jsonify(resultado)
def _linha_para_registro(texto, debug):
    """Converte o texto bruto de uma linha da listagem em um registro. None se nao for NT."""
    re_nt        = re.compile(r"\b(\d{6})\b")
    re_processo  = re.compile(r"\b(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})\b")
    re_data_hora = re.compile(r"\b(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\b")
    if not texto:
        return None
    celulas = [c.strip() for c in re.split(r"[\t\n]+", texto) if c.strip()]
    m_nt = re_nt.search(texto)
    if not m_nt:
        return None
    numero_nt   = m_nt.group(1)
    m_proc      = re_processo.search(texto)
    todas_datas = re_data_hora.findall(texto)
    numero_proc = m_proc.group(1) if m_proc else ""
    data_solic  = todas_datas[0] if todas_datas else ""
    if "Nota T" in texto and "emitida" in texto:
        status_site = "Nota Técnica emitida"
    elif "Aguardando" in texto:
        status_site = "Aguardando análise"
    else:
        status_site = ""
    # NT ja emitida: a data de emissao/conclusao e a data-hora MAIS recente da linha
    # (a listagem traz solicitacao e, para emitidas, tambem a emissao). So usamos quando
    # ha uma 2a data distinta; senao fica vazio e o n8n cai na data de deteccao.
    data_emissao = ""
    if status_site == "Nota Técnica emitida" and len(todas_datas) >= 2:
        data_emissao = todas_datas[-1]
    vara = ""
    for c in celulas:
        if any(k in c for k in ["Vara", "Comarca", "Núcleo", "Juizado", "Turma"]):
            vara = c
            break
    paciente = ""
    for idx, c in enumerate(celulas):
        if re_data_hora.search(c) and idx + 1 < len(celulas):
            paciente = celulas[idx + 1]
            break
    doenca_rara = "Sim" if "\tSim\t" in ("\t" + "\t".join(celulas) + "\t") else "Não"
    registro = {
        "numero_nt": numero_nt,
        "data_solicitacao": data_solic,
        "data_emissao": data_emissao,
        "paciente": paciente,
        "numero_processo": numero_proc,
        "vara": vara,
        "status_site": status_site,
        "doenca_rara": doenca_rara,
    }
    if debug:
        registro["celulas"] = celulas
    return registro


@app.route("/listar", methods=["GET"])
def listar():
    """
    Lista as NTs da pagina de listagem do e-NatJus, com campos ja separados.
    Por padrao devolve so as 'Aguardando analise' (as que precisam de parecer).

    A listagem e um DataTables (id 'tabela-solicitacao') com ~3.2 mil registros:
    a URL nao muda ao paginar, entao percorremos clicando no botao "»" e ajustamos
    o seletor "Itens por pagina" (aceita 10/25/50/100) para reduzir os cliques.

    Parametros opcionais:
      ?todos=1      -> devolve tambem as ja emitidas
      ?debug=1      -> inclui as celulas brutas de cada linha
      ?paginas=N    -> quantas paginas percorrer (default 1, teto 40)
      ?porpagina=N  -> itens por pagina: 10 | 25 | 50 | 100 (default 25)

    Os defaults reproduzem exatamente o comportamento antigo (1 pagina de 25), para
    que subir esta versao nao mude nada sozinho. Quem quiser varrer mais fundo pede
    explicitamente, ex.: /listar?todos=1&paginas=5&porpagina=100
    """
    apenas_pendentes = request.args.get("todos") != "1"
    debug = request.args.get("debug") == "1"
    try:
        max_paginas = max(1, min(int(request.args.get("paginas", "1")), 40))
    except Exception:
        max_paginas = 1
    porpagina = request.args.get("porpagina", "25")
    if porpagina not in ("10", "25", "50", "100"):
        porpagina = "100"

    with _lock_navegador:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context()
            page    = context.new_page()
            try:
                _exigir_sessao(context, page)
                page.goto(LISTA_URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                if not _check_logged_in(page):
                    # autocura: reloga uma vez e refaz
                    if _relogar_se_possivel(context, page):
                        page.goto(LISTA_URL, timeout=60000)
                        page.wait_for_load_state("networkidle", timeout=30000)
                    if not _check_logged_in(page):
                        sc = _screenshot_b64(page)
                        browser.close()
                        return jsonify({"erro": "Sessão expirada ou cookies inválidos",
                                        "screenshot": sc}), 401
                _renovar_sessao(context)  # TTL deslizante

                # espera a tabela montar
                try:
                    page.wait_for_selector("#tabela-solicitacao tbody tr", timeout=20000)
                except Exception:
                    pass

                # "Itens por pagina" -> 100 (menos cliques para cobrir o mesmo intervalo)
                try:
                    page.select_option('select[name="tabela-solicitacao_length"]', porpagina)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

                nts = []
                vistos = set()
                paginas_lidas = 0
                linhas_lidas = 0

                for _pag in range(max_paginas):
                    linhas = page.locator("#tabela-solicitacao tbody tr")
                    total = linhas.count()
                    if total == 0:
                        linhas = page.locator("table tr")
                        total = linhas.count()
                    paginas_lidas += 1
                    for i in range(total):
                        try:
                            texto = linhas.nth(i).inner_text().strip()
                        except Exception:
                            continue
                        reg = _linha_para_registro(texto, debug)
                        if not reg:
                            continue
                        linhas_lidas += 1
                        if reg["numero_nt"] in vistos:
                            continue
                        vistos.add(reg["numero_nt"])
                        if apenas_pendentes and reg["status_site"] != "Aguardando análise":
                            continue
                        nts.append(reg)

                    # avanca pelo botao "»" do DataTables; para se estiver desabilitado
                    try:
                        prox = page.locator("#tabela-solicitacao_next")
                        if prox.count() == 0:
                            break
                        classe = prox.first.get_attribute("class") or ""
                        if "disabled" in classe:
                            break
                        prox.first.locator("a").first.click()
                        page.wait_for_timeout(1500)
                    except Exception:
                        break

                browser.close()
                return jsonify({
                    "total_nts": len(nts),
                    "paginas_lidas": paginas_lidas,
                    "itens_por_pagina": porpagina,
                    "linhas_varridas": linhas_lidas,
                    "nts": nts
                })
            except Exception as e:
                sc = _screenshot_b64(page)
                try:
                    browser.close()
                except Exception:
                    pass
                return jsonify({"erro": str(e), "screenshot": sc}), 500


# ─────────────────────────────────────────────
# Conclusao da NT (favoravel / nao favoravel)
# ─────────────────────────────────────────────
# Na pagina da NT o campo e <select id="selConclusao" name="selConclusao">
# com as opcoes: "" (Selecione) | "F" (Favoravel) | "N" (Nao favoravel).
_CONCLUSAO_TEXTO = {"F": "Favorável", "N": "Não favorável", "": ""}


def _ler_conclusao(pagina):
    """Le o <select id=selConclusao> da pagina da NT. Devolve (valor, texto) ou (None, None)."""
    for seletor in ["#selConclusao", "select[name='selConclusao']"]:
        try:
            el = pagina.locator(seletor).first
            if el.count() == 0:
                continue
            valor = el.input_value(timeout=5000)
            valor = (valor or "").strip().upper()
            texto = _CONCLUSAO_TEXTO.get(valor)
            if texto is None:
                # opcao inesperada: devolve o rotulo visivel da opcao selecionada
                try:
                    texto = pagina.locator(seletor + " option:checked").first.inner_text().strip()
                except Exception:
                    texto = valor
            return valor, texto
        except Exception:
            continue
    return None, None


@app.route("/conclusao/<nt>", methods=["GET"])
def conclusao(nt):
    """
    Devolve a conclusao registrada na NT: Favoravel / Nao favoravel.
    Usado pelo NATJUS1 apenas para as NTs recem-detectadas como emitidas.
    """
    with _lock_navegador:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context()
            page    = context.new_page()
            try:
                _exigir_sessao(context, page)
                pagina_nt = _navegar_ate_pagina_nt(context, page, nt)
                valor, texto = _ler_conclusao(pagina_nt)

                # a NT emitida pode guardar o campo no formulario; tenta abrir se preciso
                if valor is None:
                    try:
                        formulario = _navegar_ate_formulario(context, page, nt)
                        valor, texto = _ler_conclusao(formulario)
                    except Exception:
                        pass

                encontrado = valor is not None
                browser.close()
                return jsonify({
                    "numeroNT": str(nt),
                    "encontrado": encontrado,
                    "conclusao_valor": (valor or ""),
                    "conclusao_favoravel": (texto or ""),
                })
            except Exception as e:
                sc = _screenshot_b64(page)
                try:
                    browser.close()
                except Exception:
                    pass
                return jsonify({"erro": str(e), "numeroNT": str(nt), "screenshot": sc}), 500


# ─────────────────────────────────────────────
# Rota [LEITURA] — despeja a NT inteira, numa carga de página só
# ─────────────────────────────────────────────
# Existe para o levantamento do histórico: 3,4 mil NTs a ~20 s de carga cada, em fila única
# por causa do _lock_navegador. Duas chamadas por NT dobrariam um trabalho de 14 horas, então
# esta rota lê TUDO de uma vez: cabeçalho, conclusão, campos do formulário e os CKEditor.
#
# Ela NÃO mapeia rótulo -> coluna de planilha, de propósito. Devolve a lista crua de campos
# com o rótulo do lado, e quem consome decide o que fazer. Mapear aqui significaria um deploy
# do Railway a cada ajuste de nome de coluna; mapear fora é editar um nó de código.
_JS_LER_NT = r"""
() => {
  const txt = e => (e ? (e.innerText || e.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const out = { titulo: '', conclusao_valor: '', conclusao_texto: '',
                ver_nota: '', tabela: [], campos: [], ricos: {}, avisos: [] };

  const h = document.querySelector('h1, h2, h3, .page-header');
  out.titulo = txt(h);

  // A tabela do cabeçalho é a única com 'Conclusão' E 'Tecnologia' na primeira linha.
  // Procurar pela posição na página quebraria em qualquer redesenho.
  for (const t of document.querySelectorAll('table')) {
    const cab = Array.from(t.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td')).map(txt);
    if (cab.some(c => /Conclus/i.test(c)) && cab.some(c => /Tecnologia/i.test(c))) {
      for (const tr of t.querySelectorAll('tbody tr, tr')) {
        const cels = Array.from(tr.querySelectorAll('td')).map(txt);
        if (!cels.length) continue;
        const a = tr.querySelector('a[href]');
        out.tabela.push({ celulas: cels, link: a ? a.href : '' });
        if (a && /Ver\s*Nota/i.test(txt(a)) && !out.ver_nota) out.ver_nota = a.href;
      }
      break;
    }
  }

  const sc = document.getElementById('selConclusao');
  if (sc) {
    out.conclusao_valor = sc.value || '';
    const op = sc.options[sc.selectedIndex];
    out.conclusao_texto = op ? txt(op) : '';
  } else {
    out.avisos.push('selConclusao ausente');
  }

  // Os campos ricos são CKEditor. O <textarea> original existe no DOM mas só é
  // sincronizado no submit — ler o textarea devolve vazio ou desatualizado, e em silêncio.
  try {
    if (window.CKEDITOR && CKEDITOR.instances) {
      for (const k in CKEDITOR.instances) {
        try { out.ricos[k] = CKEDITOR.instances[k].getData() || ''; }
        catch (e) { out.avisos.push('CKEDITOR ' + k + ': ' + e.message); }
      }
    } else {
      out.avisos.push('CKEDITOR nao encontrado na pagina');
    }
  } catch (e) { out.avisos.push('CKEDITOR: ' + e.message); }

  document.querySelectorAll('.form-group').forEach(g => {
    const lab = g.querySelector('label');
    const rotulo = txt(lab).replace(/\s*\*\s*$/, '');
    g.querySelectorAll('input, select, textarea').forEach(c => {
      const tag = c.tagName.toLowerCase();
      if (c.type === 'hidden' && !c.value) return;
      if ((c.type === 'radio' || c.type === 'checkbox') && !c.checked) return;
      const item = { rotulo: rotulo, tag: tag, tipo: c.type || '',
                     name: c.name || '', id: c.id || '', valor: '' };
      if (tag === 'select') {
        const op = c.options[c.selectedIndex];
        item.valor = op ? txt(op) : (c.value || '');
        item.valor_bruto = c.value || '';
      } else if (c.type === 'radio' || c.type === 'checkbox') {
        item.valor = c.value || 'on';
      } else {
        item.valor = c.value || '';
        if (tag === 'textarea' && out.ricos[item.name] !== undefined) item.valor = out.ricos[item.name];
      }
      out.campos.push(item);
    });
  });

  return out;
}
"""


@app.route("/nt/<nt>", methods=["GET"])
def nt_completa(nt):
    """
    SOMENTE LEITURA. Abre a página da NT e devolve tudo o que ela mostra.

    Nao clica em nada, nao preenche, nao salva. Pensada para varredura em massa.

    Parametro opcional:
      ?captura=1  -> inclui a captura de tela em base64. NAO use em lote: sao ~350 KB por NT.

    Resposta (200):
      { numeroNT, url, titulo, conclusao_valor, conclusao_texto, ver_nota,
        tabela: [{celulas, link}], campos: [{rotulo, tag, tipo, name, id, valor, valor_bruto?}],
        ricos: {nome_do_campo: html}, avisos: [], erro: "" }

    Em falha devolve 500 com {erro, numeroNT} — e, se ?captura=1, a tela. O consumidor deve
    usar onError continueRegularOutput, como o no 12 do NATJUS1 ja faz com /conclusao.
    """
    quer_captura = request.args.get("captura") == "1"
    with _lock_navegador:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context()
            page = context.new_page()
            try:
                _exigir_sessao(context, page)
                # De proposito NAO usa _navegar_ate_formulario: aquela funcao exige o texto
                # 'Diagnostico Principal' e levanta excecao se ele nao aparecer. Numa varredura
                # de 3,4 mil registros heterogeneos, isso jogaria fora paginas que tem metade
                # do que interessa. Aqui a espera e mole: se a aba nao montar, segue e avisa.
                pagina = _navegar_ate_pagina_nt(context, page, nt)
                aviso_forma = ""
                try:
                    pagina.wait_for_selector("text=Diagnóstico Principal", timeout=15000)
                except Exception:
                    aviso_forma = "aba da tecnologia nao apareceu em 15s; leitura pode estar incompleta"

                dados = pagina.evaluate(_JS_LER_NT)
                if aviso_forma:
                    dados.setdefault("avisos", []).append(aviso_forma)
                dados["numeroNT"] = str(nt)
                dados["url"] = pagina.url
                dados["erro"] = ""
                if quer_captura:
                    dados["screenshot"] = _screenshot_b64(pagina)
                browser.close()
                return jsonify(dados)
            except Exception as e:
                sc = _screenshot_b64(page) if quer_captura else ""
                try:
                    browser.close()
                except Exception:
                    pass
                return jsonify({"numeroNT": str(nt), "erro": str(e),
                                "campos": [], "ricos": {}, "tabela": [],
                                "avisos": [], "screenshot": sc}), 500


# ─────────────────────────────────────────────
# Rota [FASE 2] — preenche o formulário da NT (sem submeter)
# ─────────────────────────────────────────────
@app.route("/campos/<nt>", methods=["GET"])
@_serializado
def campos(nt):
    """
    Despeja todo campo do formulario da NT: rotulo, tag, tipo, name, id, classes e, para os
    selects, as primeiras opcoes. Nao preenche e nao grava nada.

    Existe para nao precisar adivinhar. Quando um campo falhar, esta rota diz exatamente com o
    que ele se parece no DOM, em vez de custar mais um ciclo de deploy para descobrir.
    """
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page = context.new_page()
        try:
            _exigir_sessao(context, page)
            form = _navegar_ate_formulario(context, page, nt)
            dados = form.evaluate("""() => {
                const out = [];
                document.querySelectorAll('.form-group').forEach(g => {
                    const lab = g.querySelector('label');
                    g.querySelectorAll('input, select, textarea').forEach(c => {
                        const est = window.getComputedStyle(c);
                        const item = {
                            rotulo: lab ? lab.innerText.trim().replace(/\\s+/g, ' ') : '',
                            tag: c.tagName.toLowerCase(),
                            tipo: c.type || '',
                            name: c.name || '',
                            id: c.id || '',
                            classe: c.className || '',
                            visivel: est.display !== 'none' && est.visibility !== 'hidden',
                            vizinhos: Array.from(g.querySelectorAll('div,span'))
                                .map(e => e.className).filter(Boolean).slice(0, 6)
                        };
                        if (c.tagName.toLowerCase() === 'select') {
                            item.opcoes = Array.from(c.options).slice(0, 8).map(o => o.text.trim());
                            item.total_opcoes = c.options.length;
                        }
                        out.push(item);
                    });
                });
                return out;
            }""")
            browser.close()
            return jsonify({"numeroNT": nt, "total": len(dados), "campos": dados})
        except Exception as e:
            sc = _screenshot_b64(page)
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "screenshot": sc}), 500


@app.route("/preencher", methods=["POST"])
@_serializado
def preencher():
    """
    Preenche os campos de identificacao da NT no e-NatJus e, opcionalmente, salva a tecnologia.

    Corpo:
      numeroNT                 obrigatorio
      cid                      codigo CID-10, ex. "N80.9" — a busca do e-NatJus e por codigo
      diagnostico              texto
      meios_confirmatorios     texto
      natjus_responsavel       default "Ceará"
      instituicao_responsavel  default "TJCE"
      apoio_tutoria            default "Não"
      salvar                   default False

    NUNCA clica em 'Salvar e Finalizar Tecnologia'. Finalizar e o que libera o botao
    'Realizar emissao' — enquanto a tecnologia nao e finalizada, o proprio e-NatJus segura a
    emissao, e essa trava e o que garante que nenhuma NT saia sem um humano ter lido.
    """
    dados = request.json or {}
    nt = dados.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    salvar = bool(dados.get("salvar", False))
    log = []
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page = context.new_page()
        try:
            _exigir_sessao(context, page)
            form = _navegar_ate_formulario(context, page, nt)
            log.append("Formulario da NT localizado")

            if dados.get("cid"):
                _select_por_rotulo(form, "CID", dados["cid"], log, "CID")
            if dados.get("diagnostico"):
                _texto_por_rotulo(form, "Diagnóstico", dados["diagnostico"], log, "Diagnostico")
            if dados.get("meios_confirmatorios"):
                _texto_por_rotulo(form, "Meio(s) confirmatório(s) do diagnóstico já realizado(s)",
                                  dados["meios_confirmatorios"], log, "Meios confirmatorios")

            natjus = dados.get("natjus_responsavel", "Ceará")
            _select_por_rotulo(form, "NatJus Responsável", natjus, log, "NatJus responsavel")

            inst = dados.get("instituicao_responsavel", "TJCE")
            _texto_por_rotulo(form, "Instituição Responsável", inst, log, "Instituicao responsavel")

            tutoria = dados.get("apoio_tutoria", "Não")
            _select_por_rotulo(form, "Nota técnica elaborada com apoio de tutoria?",
                               tutoria, log, "Apoio de tutoria")

            antes = _screenshot_b64(form)

            salvo = False
            if salvar:
                try:
                    botao = form.locator("button:has-text('Salvar Tecnologia'), "
                                         "a:has-text('Salvar Tecnologia'), "
                                         "input[value='Salvar Tecnologia']").first
                    botao.wait_for(state="visible", timeout=8000)
                    botao.click()
                    form.wait_for_load_state("networkidle", timeout=30000)
                    salvo = True
                    log.append("Tecnologia salva (botao 'Salvar Tecnologia')")
                except Exception as e:
                    log.append(f"Salvar tecnologia: FALHOU ({type(e).__name__}: {str(e)[:90]})")
            else:
                log.append("NAO salvo — chamada de conferencia (salvar=false)")

            depois = _screenshot_b64(form)
            falhas = [l for l in log if "FALHOU" in l or "GRAVOU DIFERENTE" in l]
            browser.close()
            return jsonify({
                "sucesso": len(falhas) == 0,
                "numeroNT": nt,
                "salvo": salvo,
                "falhas": falhas,
                "log": log,
                "screenshot": depois,
                "screenshot_antes_de_salvar": antes if salvar else None
            })
        except Exception as e:
            sc = _screenshot_b64(page)
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "log": log, "screenshot": sc}), 500

# ─────────────────────────────────────────────
# Rota [REVISAO] — preenche a NT inteira no e-NatJus a partir do rascunho revisado
# ─────────────────────────────────────────────
# (20/09/2026) Ate aqui o /preencher so sabia os campos de identificacao (CID, diagnostico,
# meios confirmatorios, NatJus responsavel, instituicao, tutoria). Esta rota cobre TODO o
# formulario da tecnologia — selects fechados, buscas select2 (principio ativo, nome comercial,
# procedimento), textos, precos com mascara e os 8 campos ricos em CKEditor — a partir de um
# dicionario {id_do_campo: valor} que a pagina de revisao monta depois do parecerista confirmar
# campo a campo.
#
# O que ela NUNCA faz: clicar em 'Salvar e Finalizar Tecnologia'. Finalizar e o que libera a
# emissao; enquanto so 'Salvar Tecnologia' e usado, o proprio e-NatJus segura a NT ate um humano
# entrar la e finalizar/emitir. Com salvar=false (default) ela nem salva: preenche, le de volta
# e devolve o que ficaria em cada campo, com captura de tela — e o modo de conferencia.
#
# Inventario dos campos: GET /campos/<nt> (a rota abaixo usa os ids de la).

# id -> (tipo, rotulo humano). A ordem AQUI e a ordem de preenchimento: os selects que
# mostram/escondem outros campos (tipo da tecnologia, registro ANVISA, SUS, generico/similar,
# urgencia) vem antes dos campos que dependem deles.
CAMPOS_NT = [
    ("selTipoTecnologia", "select", "Tipo da Tecnologia"),
    ("selRegistroAnvisa", "select", "Registro na ANVISA?"),
    ("selSituacaoAnvisa", "select", "Situação do registro"),
    ("txtDcb", "select2", "Princípio Ativo"),
    ("txtDcbComercial", "select2", "Nome comercial"),
    ("txtProcedimento", "select2", "Descrição (procedimento)"),
    ("txtProduto", "texto", "Descrição (produto)"),
    ("txtViaAdministracao", "texto", "Via de administração"),
    ("txaPosologia", "texto", "Posologia"),
    ("selUsoContinuo", "select", "Uso contínuo?"),
    ("txtDuracaoTratamento", "inteiro", "Duração do tratamento"),
    ("selUnidadeDuracaoTratamento", "select", "Duração do tratamento (unidade)"),
    ("selIndicacaoConformidade", "select", "Indicação em conformidade com o registro?"),
    ("selPrevistoProtocolo", "select", "Previsto em PCDT?"),
    ("selDisponivelSus", "select", "Inserido no SUS?"),
    ("selTabelaTecnologia", "select", "Incluído em (RENAME/REMUME/SIGTAP/CIB)"),
    ("selOncologico", "select", "Oncológico?"),
    ("txaOpcaoSus", "ckeditor", "Opções disponíveis no SUS e/ou Saúde Suplementar"),
    ("selExisteGenerico", "select", "Existe Genérico?"),
    ("selExisteBiossimilar", "select", "Existe Similar?"),
    ("txaGenericoBiossimilar", "ckeditor", "Opções de Genérico ou Similar"),
    ("txtLaboratorio", "texto", "Laboratório"),
    ("txtMarcaComercial", "texto", "Marca Comercial"),
    ("txtApresentacao", "texto", "Apresentação"),
    ("txtPrecoFabrica", "dinheiro", "Preço de Fábrica"),
    ("txtPrecoMaximoGoverno", "dinheiro", "PMVG (apresentação)"),
    ("txtPrecoMaximoConsumidor", "dinheiro", "PMC (apresentação)"),
    ("txtDoseDiariaRecomendada", "texto", "Dose Diária Recomendada"),
    ("txtPrecoMaximoGovernoTratamentoMensal", "dinheiro", "PMVG (tratamento mensal)"),
    ("txtPrecoMaximoConsumidorTratamentoMensal", "dinheiro", "PMC (tratamento mensal)"),
    ("txtCustoTecnologia", "ckeditor", "Custo da tecnologia"),
    ("txtFonteCusto", "texto", "Fonte do custo da tecnologia"),
    ("txaEficaciaSeguranca", "ckeditor", "Evidências sobre a eficácia e segurança"),
    ("txaImpactoTecnologia", "ckeditor", "Benefício/efeito/resultado esperado"),
    ("selRecomendacaoConitec", "select", "Recomendações da CONITEC"),
    ("selConclusao", "select", "Conclusão Justificada"),
    ("txaConclusao", "ckeditor", "Conclusão"),
    ("selEvidenciaCientifica", "select", "Há evidências científicas?"),
    ("selAlegacaoUrgencia", "select", "Justifica-se a alegação de urgência?"),
    ("selAlegacaoUrgenciaJustificativaNotaTecnica", "select", "Justificativa da urgência"),
    ("txaReferencia", "ckeditor", "Referências bibliográficas"),
    ("txaOutraInformacao", "ckeditor", "Outras Informações"),
]
CAMPOS_NT_POR_ID = {c[0]: (c[1], c[2]) for c in CAMPOS_NT}


def _norm_txt(s):
    """minusculas, sem acento, espacos colapsados — para comparar rotulos e opcoes."""
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _html_de_texto(valor):
    """Texto plano -> HTML de paragrafos (uma linha em branco separa paragrafos; quebra simples
    vira <br>). Se ja vier com tags, passa como esta."""
    v = str(valor or "")
    if re.search(r"<\s*(p|br|ul|ol|li|table|b|strong|i|em|h\d)\b", v, re.IGNORECASE):
        return v
    def esc(t):
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    blocos = [b.strip() for b in re.split(r"\n\s*\n", v.replace("\r\n", "\n")) if b.strip()]
    return "".join("<p>" + esc(b).replace("\n", "<br>") + "</p>" for b in blocos)


def _texto_de_html(h):
    t = re.sub(r"<\s*br\s*/?>", "\n", str(h or ""), flags=re.IGNORECASE)
    t = re.sub(r"</\s*(p|div|li|tr|h\d)\s*>", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    import html as _html
    t = _html.unescape(t).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def _centavos(valor):
    """'1.234,56' | '1234.56' | 1234.56 -> 123456 (inteiro em centavos). None se nao parsear."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return int(round(float(valor) * 100))
    s = re.sub(r"[^\d.,]", "", str(valor))
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except Exception:
        return None


def _visivel(pagina, id_campo, tipo):
    """O campo esta na tela? Selects/inputs: o proprio elemento. select2: o widget ao lado.
    CKEditor: o container cke_<id>. Campos escondidos pelo tipo da tecnologia sao pulados."""
    try:
        if tipo == "select2":
            return pagina.evaluate("""(id) => {
                const el = document.getElementById(id); if (!el) return false;
                const w = el.nextElementSibling && el.nextElementSibling.classList && el.nextElementSibling.classList.contains('select2') ? el.nextElementSibling : null;
                const alvo = w || el;
                return !!(alvo.offsetParent) ;
            }""", id_campo)
        if tipo == "ckeditor":
            return pagina.evaluate("""(id) => {
                const c = document.getElementById('cke_' + id);
                if (c) return !!c.offsetParent;
                const el = document.getElementById(id); return !!(el && el.offsetParent);
            }""", id_campo)
        return pagina.evaluate("(id) => { const el = document.getElementById(id); return !!(el && el.offsetParent); }", id_campo)
    except Exception:
        return False


def _ler_select(pagina, id_campo):
    return pagina.evaluate("""(id) => { const el = document.getElementById(id); if (!el) return null;
        const op = el.options[el.selectedIndex]; return op ? op.text.trim() : ''; }""", id_campo)


def _preencher_select_id(pagina, id_campo, valor, log, nome):
    """<select> comum: casa a opcao por texto normalizado (sem acento/caixa) ou por value."""
    alvo = _norm_txt(valor)
    try:
        opcoes = pagina.evaluate("""(id) => { const el = document.getElementById(id); if (!el) return null;
            return Array.from(el.options).map(o => ({v: o.value, t: o.text.trim()})); }""", id_campo)
        if opcoes is None:
            log.append(f"{nome}: FALHOU (select #{id_campo} nao existe)")
            return {"status": "FALHOU", "motivo": "campo nao existe"}
        escolha = None
        for o in opcoes:
            if _norm_txt(o["t"]) == alvo or _norm_txt(o["v"]) == alvo:
                escolha = o
                break
        if escolha is None:
            # aceita prefixo inequivoco ("Nao" -> "Não"; "Recomendada" nao pode casar "Não Recomendada")
            cands = [o for o in opcoes if _norm_txt(o["t"]).startswith(alvo) and alvo]
            if len(cands) == 1:
                escolha = cands[0]
        if escolha is None:
            disp = [o["t"] for o in opcoes if o["t"]]
            log.append(f"{nome}: FALHOU (opcao '{valor}' nao existe; opcoes: {', '.join(disp)[:120]})")
            return {"status": "FALHOU", "motivo": "opcao inexistente", "opcoes": disp}
        pagina.select_option(f"#{id_campo}", value=escolha["v"], timeout=5000)
        pagina.wait_for_timeout(250)
        lido = _ler_select(pagina, id_campo)
        ok = _norm_txt(lido) == _norm_txt(escolha["t"])
        log.append(f"{nome}: {'OK' if ok else 'GRAVOU DIFERENTE'} ({lido})")
        return {"status": "OK" if ok else "FALHOU", "lido": lido}
    except Exception as e:
        log.append(f"{nome}: FALHOU ({type(e).__name__}: {str(e)[:90]})")
        return {"status": "FALHOU", "motivo": str(e)[:120]}


def _preencher_select2_id(pagina, id_campo, valor, log, nome, exigir_exato=True, contexto=""):
    """Combobox select2 com busca remota (principio ativo, nome comercial, procedimento).
    Abre o widget, digita, lista TODAS as opcoes devolvidas e escolhe a que casa exatamente com o
    valor (normalizado). Sem casamento exato: com exigir_exato=True devolve AMBIGUO/SEM_RESULTADO
    e a lista de candidatos (a pagina de revisao mostra para o parecerista escolher); com
    exigir_exato=False pega a primeira e marca 'aproximado'."""
    alvo = str(valor or "").strip()
    try:
        gatilho = pagina.locator(f"#select2-{id_campo}-container").first
        if gatilho.count() == 0:
            gatilho = pagina.locator(f"#{id_campo} + span.select2 .select2-selection:visible").first
        gatilho.wait_for(state="visible", timeout=8000)
        gatilho.click()
        pagina.wait_for_timeout(400)
        busca = pagina.locator("input.select2-search__field:visible").last
        try:
            busca.fill(alvo, timeout=4000)
        except Exception:
            pagina.keyboard.type(alvo, delay=40)
        # espera a busca remota devolver algo alem de 'Carregando'
        fim = time.time() + 12
        cands = []
        while time.time() < fim:
            pagina.wait_for_timeout(500)
            cands = pagina.evaluate("""() => Array.from(document.querySelectorAll('.select2-results__option'))
                .map(e => (e.innerText || '').trim()).filter(Boolean)""")
            if cands and not any(re.search(r"carregando|searching|loading", c, re.IGNORECASE) for c in cands):
                break
        cands = [c for c in cands if c]
        sem = [c for c in cands if re.search(r"nenhum resultado|no results", c, re.IGNORECASE)]
        if not cands or sem:
            pagina.keyboard.press("Escape")
            log.append(f"{nome}: SEM_RESULTADO (busca por '{alvo[:40]}')")
            return {"status": "SEM_RESULTADO", "candidatos": []}
        # As listas do e-NatJus vem como 'NOME | COMPLEMENTO' (ex.: 'JAKAVI | FOSFATO DE RUXOLITINIBE').
        # Casa pelo texto inteiro, depois pelo primeiro segmento; empate de segmento desempata pelo
        # contexto (o principio ativo ja escolhido no txtDcb), senao fica AMBIGUO.
        def seg1(c):
            return _norm_txt(c.split("|")[0])
        exatos = [c for c in cands if _norm_txt(c) == _norm_txt(alvo)]
        por_seg = [c for c in cands if seg1(c) == _norm_txt(alvo)]
        escolhida = None
        aproximado = False
        if exatos:
            escolhida = exatos[0]
        elif len(por_seg) == 1:
            escolhida = por_seg[0]
            aproximado = True
        elif len(por_seg) > 1 and contexto:
            com_ctx = [c for c in por_seg if _norm_txt(contexto) and _norm_txt(contexto) in _norm_txt(c)]
            if len(com_ctx) == 1:
                escolhida = com_ctx[0]
                aproximado = True
        if escolhida is None and len(cands) == 1:
            escolhida = cands[0]
            aproximado = _norm_txt(cands[0]) != _norm_txt(alvo)
        if escolhida is None and not exigir_exato:
            escolhida = cands[0]
            aproximado = True
        if escolhida is None:
            pagina.keyboard.press("Escape")
            log.append(f"{nome}: AMBIGUO ({len(cands)} opcoes para '{alvo[:40]}')")
            return {"status": "AMBIGUO", "candidatos": cands[:15]}
        opcao = pagina.locator(".select2-results__option").filter(has_text=re.compile(r"^\s*" + re.escape(escolhida) + r"\s*$")).first
        if opcao.count() == 0:
            opcao = pagina.locator(".select2-results__option:visible").first
        opcao.click()
        pagina.wait_for_timeout(500)
        lido = pagina.evaluate("""(id) => { const c = document.getElementById('select2-' + id + '-container');
            return c ? (c.getAttribute('title') || c.innerText || '').trim() : ''; }""", id_campo)
        ok = _norm_txt(lido) == _norm_txt(escolhida) or _norm_txt(escolhida) in _norm_txt(lido)
        st = "OK" if ok else "FALHOU"
        if ok and aproximado:
            st = "APROXIMADO"
        log.append(f"{nome}: {st} ({lido[:60]})")
        return {"status": st, "lido": lido, "candidatos": cands[:15]}
    except Exception as e:
        try:
            pagina.keyboard.press("Escape")
        except Exception:
            pass
        log.append(f"{nome}: FALHOU ({type(e).__name__}: {str(e)[:90]})")
        return {"status": "FALHOU", "motivo": str(e)[:120]}


def _preencher_texto_id(pagina, id_campo, valor, log, nome, tipo):
    """input de texto, textarea simples, inteiro ou dinheiro (input-money: digita so os digitos,
    a mascara formata; confere pelos centavos)."""
    try:
        campo = pagina.locator(f"#{id_campo}").first
        campo.wait_for(state="visible", timeout=8000)
        if tipo == "dinheiro":
            cents = _centavos(valor)
            if cents is None:
                log.append(f"{nome}: FALHOU (valor monetario invalido: {str(valor)[:30]})")
                return {"status": "FALHOU", "motivo": "valor invalido"}
            campo.click()
            campo.fill("")
            pagina.keyboard.type(str(cents), delay=15)
            campo.dispatch_event("change")
            campo.press("Tab")
            pagina.wait_for_timeout(200)
            lido = campo.input_value()
            ok = _centavos(lido) == cents
            if not ok:
                # mascara ausente: tenta o texto formatado
                fmt = f"{cents // 100:,}".replace(",", ".") + "," + f"{cents % 100:02d}"
                campo.fill(fmt)
                campo.dispatch_event("change")
                pagina.wait_for_timeout(200)
                lido = campo.input_value()
                ok = _centavos(lido) == cents
            log.append(f"{nome}: {'OK' if ok else 'GRAVOU DIFERENTE'} ({lido})")
            return {"status": "OK" if ok else "FALHOU", "lido": lido}
        if tipo == "inteiro":
            v = re.sub(r"\D", "", str(valor))
            if not v:
                log.append(f"{nome}: FALHOU (inteiro vazio: {str(valor)[:30]})")
                return {"status": "FALHOU", "motivo": "inteiro invalido"}
            valor = v
        campo.fill("")
        campo.fill(str(valor))
        campo.dispatch_event("change")
        lido = campo.input_value()
        ok = lido.strip() == str(valor).strip()
        log.append(f"{nome}: {'OK' if ok else 'GRAVOU DIFERENTE'} ({lido[:50]})")
        return {"status": "OK" if ok else "FALHOU", "lido": lido[:200]}
    except Exception as e:
        log.append(f"{nome}: FALHOU ({type(e).__name__}: {str(e)[:90]})")
        return {"status": "FALHOU", "motivo": str(e)[:120]}


def _preencher_ckeditor_id(pagina, id_campo, valor, log, nome):
    """Campo rico: escreve pela API do CKEditor (setData) e sincroniza o <textarea>; le de volta
    com getData. Sem instancia, cai para o textarea (o e-NatJus le o textarea no submit)."""
    html = _html_de_texto(valor)
    try:
        r = pagina.evaluate("""([id, html]) => {
            if (window.CKEDITOR && CKEDITOR.instances && CKEDITOR.instances[id]) {
                const ed = CKEDITOR.instances[id];
                ed.setData(html);
                try { ed.updateElement(); } catch (e) {}
                return { modo: 'ckeditor', lido: ed.getData() || '' };
            }
            const el = document.getElementById(id);
            if (!el) return { modo: 'ausente', lido: '' };
            el.value = html;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return { modo: 'textarea', lido: el.value };
        }""", [id_campo, html])
        if r["modo"] == "ausente":
            log.append(f"{nome}: FALHOU (campo #{id_campo} nao existe)")
            return {"status": "FALHOU", "motivo": "campo nao existe"}
        # setData e assincrono em algumas versoes: espera ate o getData refletir
        esperado = _texto_de_html(html)
        lido = _texto_de_html(r["lido"])
        fim = time.time() + 6
        while esperado and _norm_txt(lido)[:200] != _norm_txt(esperado)[:200] and time.time() < fim:
            pagina.wait_for_timeout(400)
            lido = _texto_de_html(pagina.evaluate("""(id) => (window.CKEDITOR && CKEDITOR.instances && CKEDITOR.instances[id]) ? (CKEDITOR.instances[id].getData() || '') : ((document.getElementById(id) || {}).value || '')""", id_campo))
        ok = (not esperado) or _norm_txt(lido)[:200] == _norm_txt(esperado)[:200]
        log.append(f"{nome}: {'OK' if ok else 'GRAVOU DIFERENTE'} ({r['modo']}, {len(lido)} caracteres)")
        return {"status": "OK" if ok else "FALHOU", "modo": r["modo"], "caracteres": len(lido), "lido": lido[:300]}
    except Exception as e:
        log.append(f"{nome}: FALHOU ({type(e).__name__}: {str(e)[:90]})")
        return {"status": "FALHOU", "motivo": str(e)[:120]}


def _ler_campos_nt(pagina):
    """Le o estado atual de todos os campos de CAMPOS_NT (para a conferencia e para o
    'depois de salvar'). Ricos: texto plano truncado + tamanho."""
    return pagina.evaluate("""(lista) => {
      const out = {};
      const norm = h => String(h || '').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\\s+/g, ' ').trim();
      for (const [id, tipo] of lista) {
        const el = document.getElementById(id);
        if (!el) { out[id] = { existe: false }; continue; }
        const vis = !!(el.offsetParent) || (tipo === 'select2' && el.nextElementSibling && !!el.nextElementSibling.offsetParent) || (tipo === 'ckeditor' && document.getElementById('cke_' + id) && !!document.getElementById('cke_' + id).offsetParent);
        let valor = '';
        if (tipo === 'select') { const op = el.options[el.selectedIndex]; valor = op ? op.text.trim() : ''; }
        else if (tipo === 'select2') { const c = document.getElementById('select2-' + id + '-container'); valor = c ? (c.getAttribute('title') || c.innerText || '').trim() : ''; if (/^(selecione|digite)/i.test(valor)) valor = ''; }
        else if (tipo === 'ckeditor') { let h = ''; try { h = (window.CKEDITOR && CKEDITOR.instances[id]) ? CKEDITOR.instances[id].getData() : el.value; } catch (e) { h = el.value; } const t = norm(h); out[id] = { existe: true, visivel: vis, valor: t.slice(0, 300), caracteres: t.length }; continue; }
        else { valor = el.value || ''; }
        out[id] = { existe: true, visivel: vis, valor: String(valor).slice(0, 300) };
      }
      return out;
    }""", [[c[0], c[1]] for c in CAMPOS_NT])


@app.route("/preencher-nt", methods=["POST"])
@_serializado
def preencher_nt():
    """
    Preenche o formulario da tecnologia da NT com os valores revisados e, se salvar=true, clica
    em 'Salvar Tecnologia' (e SOMENTE nele).

    Corpo:
      numeroNT        obrigatorio
      campos          {id: valor} — ids de CAMPOS_NT (ver GET /campos/<nt>). Valor de select =
                      texto da opcao ('Não favorável'); select2 = termo a buscar; dinheiro =
                      '1.234,56' ou 1234.56; ricos = HTML ou texto plano (linhas viram <p>).
      salvar          default false: preenche, confere e devolve sem gravar
      exigir_exato    default true: buscas select2 so aceitam casamento exato (senao AMBIGUO)
      ler_antes       default true: devolve o estado dos campos antes de mexer
      captura         default true: screenshots (antes de salvar / final)

    Resposta:
      { sucesso, numeroNT, salvo, resultado: {id: {status, lido, candidatos?}}, falhas: [ids],
        pulados: [ids ocultos], antes: {...}, depois: {...}, log: [...], screenshot, screenshot_antes_de_salvar }
      status por campo: OK | APROXIMADO | AMBIGUO | SEM_RESULTADO | OCULTO | FALHOU
    """
    dados = request.json or {}
    nt = str(dados.get("numeroNT") or "").strip()
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400
    campos = dados.get("campos") or {}
    if not isinstance(campos, dict) or not campos:
        return jsonify({"erro": "campos {id: valor} obrigatorio"}), 400
    desconhecidos = [k for k in campos if k not in CAMPOS_NT_POR_ID]
    salvar = bool(dados.get("salvar", False))
    exigir_exato = bool(dados.get("exigir_exato", True))
    ler_antes = bool(dados.get("ler_antes", True))
    captura = bool(dados.get("captura", True))
    log = []
    if desconhecidos:
        log.append("ids ignorados (fora de CAMPOS_NT): " + ", ".join(desconhecidos)[:200])

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page = context.new_page()
        try:
            _exigir_sessao(context, page)
            form = _navegar_ate_formulario(context, page, nt)
            log.append("Formulario da NT localizado")
            antes = _ler_campos_nt(form) if ler_antes else None

            resultado = {}
            pulados = []
            for id_campo, tipo, nome in CAMPOS_NT:
                if id_campo not in campos:
                    continue
                valor = campos[id_campo]
                if valor is None or (isinstance(valor, str) and not valor.strip()):
                    resultado[id_campo] = {"status": "VAZIO"}
                    continue
                if not _visivel(form, id_campo, tipo):
                    pulados.append(id_campo)
                    resultado[id_campo] = {"status": "OCULTO"}
                    log.append(f"{nome}: OCULTO (nao se aplica a este tipo/resposta) — pulado")
                    continue
                if tipo == "select":
                    r = _preencher_select_id(form, id_campo, valor, log, nome)
                    form.wait_for_timeout(300)   # deixa o JS do e-NatJus mostrar/esconder dependentes
                elif tipo == "select2":
                    ctx = ""
                    if id_campo == "txtDcbComercial":
                        ctx = str((resultado.get("txtDcb") or {}).get("lido") or campos.get("txtDcb") or "")
                    r = _preencher_select2_id(form, id_campo, valor, log, nome, exigir_exato, ctx)
                elif tipo == "ckeditor":
                    r = _preencher_ckeditor_id(form, id_campo, valor, log, nome)
                else:
                    r = _preencher_texto_id(form, id_campo, valor, log, nome, tipo)
                resultado[id_campo] = r

            falhas = [k for k, v in resultado.items() if v.get("status") in ("FALHOU", "AMBIGUO", "SEM_RESULTADO")]
            depois_preencher = _ler_campos_nt(form)
            shot_antes = _screenshot_b64(form) if captura else None

            salvo = False
            depois_salvar = None
            if salvar:
                if falhas:
                    log.append(f"NAO salvo: {len(falhas)} campo(s) com falha — corrija e chame de novo")
                else:
                    try:
                        botao = form.locator("button, a, input[type='submit'], input[type='button']").filter(
                            has_text=re.compile(r"^\s*Salvar\s+Tecnologia\s*$", re.IGNORECASE)).first
                        if botao.count() == 0:
                            botao = form.locator("input[value='Salvar Tecnologia']").first
                        botao.wait_for(state="visible", timeout=8000)
                        botao.click()
                        try:
                            form.wait_for_load_state("networkidle", timeout=30000)
                        except Exception:
                            pass
                        form.wait_for_timeout(1500)
                        salvo = True
                        log.append("Tecnologia salva (botao 'Salvar Tecnologia'); NAO finalizada — a emissao continua com o humano")
                        try:
                            form.wait_for_selector("text=Diagnóstico Principal", timeout=15000)
                            depois_salvar = _ler_campos_nt(form)
                        except Exception:
                            depois_salvar = None
                    except Exception as e:
                        log.append(f"Salvar tecnologia: FALHOU ({type(e).__name__}: {str(e)[:90]})")
            else:
                log.append("NAO salvo — chamada de conferencia (salvar=false)")

            shot_fim = _screenshot_b64(form) if (captura and salvar) else shot_antes
            browser.close()
            return jsonify({
                "sucesso": len(falhas) == 0 and (salvo or not salvar),
                "numeroNT": nt,
                "salvo": salvo,
                "resultado": resultado,
                "falhas": falhas,
                "pulados": pulados,
                "antes": antes,
                "depois": depois_salvar if depois_salvar is not None else depois_preencher,
                "log": log,
                "screenshot": shot_fim,
                "screenshot_antes_de_salvar": shot_antes if salvar else None
            })
        except Exception as e:
            sc = _screenshot_b64(page) if captura else None
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "numeroNT": nt, "log": log, "screenshot": sc}), 500


@app.route("/opcoes-nt", methods=["POST"])
@_serializado
def opcoes_nt():
    """
    Lista as opcoes que a busca do e-NatJus devolve para um campo select2 (principio ativo, nome
    comercial ou procedimento), SEM escolher nada. A pagina de revisao usa isto para o parecerista
    apontar o registro certo quando a busca e ambigua.

    Corpo: { numeroNT, campo: 'txtDcb'|'txtDcbComercial'|'txtProcedimento', termo }
    Resposta: { numeroNT, campo, termo, candidatos: [...] }
    """
    dados = request.json or {}
    nt = str(dados.get("numeroNT") or "").strip()
    campo = str(dados.get("campo") or "").strip()
    termo = str(dados.get("termo") or "").strip()
    if not nt or not termo or CAMPOS_NT_POR_ID.get(campo, ("",))[0] != "select2":
        return jsonify({"erro": "numeroNT, termo e campo select2 (txtDcb|txtDcbComercial|txtProcedimento) obrigatorios"}), 400
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page = context.new_page()
        try:
            _exigir_sessao(context, page)
            form = _navegar_ate_formulario(context, page, nt)
            if not _visivel(form, campo, "select2"):
                browser.close()
                return jsonify({"numeroNT": nt, "campo": campo, "termo": termo, "candidatos": [], "aviso": "campo oculto para o tipo de tecnologia atual"})
            gatilho = form.locator(f"#select2-{campo}-container").first
            gatilho.wait_for(state="visible", timeout=8000)
            gatilho.click()
            form.wait_for_timeout(400)
            busca = form.locator("input.select2-search__field:visible").last
            busca.fill(termo, timeout=4000)
            fim = time.time() + 12
            cands = []
            while time.time() < fim:
                form.wait_for_timeout(500)
                cands = form.evaluate("""() => Array.from(document.querySelectorAll('.select2-results__option'))
                    .map(e => (e.innerText || '').trim()).filter(Boolean)""")
                if cands and not any(re.search(r"carregando|searching|loading", c, re.IGNORECASE) for c in cands):
                    break
            form.keyboard.press("Escape")
            cands = [c for c in cands if c and not re.search(r"nenhum resultado|no results", c, re.IGNORECASE)]
            browser.close()
            return jsonify({"numeroNT": nt, "campo": campo, "termo": termo, "candidatos": cands[:30]})
        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "numeroNT": nt, "campo": campo, "termo": termo}), 500


# ─────────────────────────────────────────────
# Rota [LEGADO] — extrai texto de um PDF em base64
# ─────────────────────────────────────────────
@app.route("/comprimir", methods=["POST"])
def comprimir():
    """(LEGADO) Extrai texto de um PDF base64. Hoje /processar já faz isso interno."""
    try:
        import io
        try:
            import pdfplumber
        except ImportError:
            import subprocess
            subprocess.run(["pip", "install", "pdfplumber", "-q"], check=True)
            import pdfplumber
        dados = request.json
        pdf_base64 = dados.get("pdfBase64")
        numero_nt  = dados.get("numeroNT", "")
        if not pdf_base64:
            return jsonify({"erro": "pdfBase64 obrigatorio"}), 400
        pdf_bytes = base64.b64decode(pdf_base64)
        texto = ""
        total_paginas = 0
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_paginas = len(pdf.pages)
            for page in pdf.pages:
                texto += page.extract_text() or ""
                texto += "\n\n"
        texto = texto.strip()
        if not texto:
            return jsonify({"erro": "Nenhum texto extraído — PDF pode ser imagem escaneada"}), 422
        return jsonify({
            "numeroNT":   numero_nt,
            "texto":      texto,
            "caracteres": len(texto),
            "paginas":    total_paginas
        })
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
