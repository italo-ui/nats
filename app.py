import os, time, base64, json, subprocess, urllib.request, urllib.error, re
import io
try:
    import fitz  # PyMuPDF (rasteriza paginas; pip puro, sem libs de sistema)
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
import threading
_lock_navegador = threading.Lock()

ENATJUS_BASE = "https://www.pje.jus.br/e-natjus"
LOGIN_URL     = f"{ENATJUS_BASE}/index.php"
LISTA_URL     = f"{ENATJUS_BASE}/notaTecnica-solicitacao-listar.php"
DADOS_URL     = f"{ENATJUS_BASE}/notaTecnica-dados.php?idNotaTecnica={{nt}}"
DOWNLOAD_URL  = f"{ENATJUS_BASE}/arquivo-download.php?hash={{hash}}"

COOKIES_JSON_ENV = "ENATJUS_COOKIES"


def _launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--single-process", "--no-zygote", "--disable-setuid-sandbox",
            "--disable-extensions", "--memory-pressure-off"
        ]
    )


def _inject_cookies(context):
    raw = os.environ.get(COOKIES_JSON_ENV, "[]")
    cookies = json.loads(raw)
    playwright_cookies = []
    for c in cookies:
        pc = {
            "name":   c.get("name", ""),
            "value":  c.get("value", ""),
            "domain": c.get("domain", "www.pje.jus.br"),
            "path":   c.get("path", "/"),
        }
        if "secure" in c:   pc["secure"]   = bool(c["secure"])
        if "httpOnly" in c: pc["httpOnly"] = bool(c["httpOnly"])
        if "sameSite" in c:
            ss = c["sameSite"]
            if isinstance(ss, str) and ss in ("Strict", "Lax", "None"):
                pc["sameSite"] = ss
        if "expirationDate" in c:
            pc["expires"] = float(c["expirationDate"])
        playwright_cookies.append(pc)
    if playwright_cookies:
        context.add_cookies(playwright_cookies)
    return len(playwright_cookies)


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
        raise Exception("Sessão expirada ou cookies inválidos")

    if "notaTecnica-solicitacao-listar" in page.url:
        raise Exception(f"NT {nt} não encontrada — redirecionado para listagem")

    _aguardar_conteudo(page)
    return page


def _navegar_via_listagem(context, page, nt):
    page.goto(LISTA_URL, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)

    if not _check_logged_in(page):
        raise Exception("Sessão expirada ou cookies inválidos")

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


def _baixar_arquivo(hash_val, cookie_str):
    url = DOWNLOAD_URL.format(hash=hash_val)
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie_str)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    req.add_header("Referer", ENATJUS_BASE + "/")
    with urllib.request.urlopen(req, timeout=60) as resp:
        conteudo = resp.read()
        content_type = resp.headers.get("Content-Type", "")
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
    Navega até a página da NT e clica no botão/link para abrir o formulário
    de preenchimento da nota técnica.
    """
    pagina_nt = _navegar_ate_pagina_nt(context, page, nt)

    for seletor in [
        "a:has-text('Preencher')",
        "a:has-text('Elaborar')",
        "button:has-text('Preencher')",
        "button:has-text('Elaborar')",
        "a:has-text('Nota Técnica')",
        ".btn:has-text('Preencher')",
    ]:
        try:
            pagina_nt.wait_for_selector(seletor, timeout=5000)
            with context.expect_page() as nova_pagina_info:
                pagina_nt.click(seletor)
            formulario = nova_pagina_info.value
            formulario.wait_for_load_state("networkidle", timeout=30000)
            return formulario
        except Exception:
            pass

    for seletor in [
        "a:has-text('Preencher')",
        "a:has-text('Elaborar')",
        "button:has-text('Preencher')",
    ]:
        try:
            pagina_nt.click(seletor)
            pagina_nt.wait_for_load_state("networkidle", timeout=30000)
            return pagina_nt
        except Exception:
            pass

    raise Exception("Não foi possível localizar o botão de preenchimento do formulário")


# ─────────────────────────────────────────────
# Rotas básicas
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/teste", methods=["GET"])
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
        return jsonify({
            "playwright": "OK" if result.returncode == 0 else "ERRO",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()
        })
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/login", methods=["GET"])
def login():
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page    = context.new_page()
        try:
            n_cookies = _inject_cookies(context)
            page.goto(LOGIN_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            autenticado = _check_logged_in(page)
            resultado = {
                "cookies_injetados": n_cookies,
                "autenticado": autenticado,
                "url_final": page.url,
                "html": page.content()[:5000],
                "screenshot": _screenshot_b64(page)
            }
            browser.close()
            return jsonify(resultado)
        except Exception as e:
            sc = _screenshot_b64(page)
            browser.close()
            return jsonify({"erro": str(e), "screenshot": sc}), 500


@app.route("/diagnostico/<nt>", methods=["GET"])
def diagnostico(nt):
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()
        try:
            _inject_cookies(context)
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
            n_cookies = _inject_cookies(context)
            if n_cookies == 0:
                browser.close()
                return jsonify({"erro": "Nenhum cookie configurado."}), 500

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


def _ocr_pagina(doc_fitz, indice, dpi=300):
    """Renderiza a pagina em alta resolucao, pre-processa e roda OCR (portugues)."""
    if fitz is None or pytesseract is None or Image is None:
        return ""
    page = doc_fitz.load_page(indice)
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    img = _preprocess(img)
    return pytesseract.image_to_string(img, lang="por", config="--oem 1 --psm 6") or ""


@app.route("/processar", methods=["POST"])
def processar():
    """
    [PRINCIPAL] Baixa o PDF da NT E extrai o texto, tudo internamente.
    Devolve SÓ o texto (leve) — o n8n nunca recebe o arquivo pesado.
    Mede a legibilidade (ok / parcial / ilegivel) para a triagem decidir.
    """
    import io
    try:
        import pdfplumber
    except ImportError:
        subprocess.run(["pip", "install", "pdfplumber", "-q"], check=True)
        import pdfplumber

    nt = request.json.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    with _lock_navegador:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context(accept_downloads=True)
            page    = context.new_page()
            try:
                n_cookies = _inject_cookies(context)
                if n_cookies == 0:
                    browser.close()
                    return jsonify({"erro": "Nenhum cookie configurado"}), 500

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

    # baixa todos os arquivos; usa texto nativo (pdfplumber) onde der e faz OCR
    # SELETIVO so nas paginas escaneadas, com orcamento de tempo e de paginas.
    # Processa do menor para o maior (laudo/relatorio costuma ser o arquivo menor).
    texto_total = ""
    arquivos = 0
    paginas_total = 0
    paginas_ilegiveis = 0
    paginas_ocr = 0
    MAX_PAGINAS_OCR = 30      # teto de paginas que farao OCR
    OCR_TIME_BUDGET = 150     # segundos: para de fazer OCR depois disso (margem p/ timeout)
    t_inicio = time.time()

    # 1) baixa o conteudo de cada arquivo
    baixados = []
    for idx, info in enumerate(hashes_validos[:5]):
        try:
            conteudo, _ct = _baixar_arquivo(info["hash"], cookies)
            baixados.append((idx, conteudo))
        except Exception as e:
            texto_total += f"\n[erro ao baixar arquivo {idx+1}: {e}]\n"

    # 2) menor -> maior (gasta o orcamento de OCR nos arquivos menores primeiro)
    baixados.sort(key=lambda x: len(x[1]))

    for idx, conteudo in baixados:
        try:
            textos = []
            with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
                for pg in pdf.pages:
                    textos.append(pg.extract_text() or "")

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

    texto_total = texto_total.strip()

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

    return jsonify({
        "numeroNT": nt,
        "texto": texto_total,
        "caracteres": len(texto_total),
        "arquivos": arquivos,
        "paginas_total": paginas_total,
        "paginas_ilegiveis": paginas_ilegiveis,
        "paginas_ocr": paginas_ocr,
        "pct_ilegivel": pct_ilegivel,
        "legibilidade": legibilidade
    })


@app.route("/listar", methods=["GET"])
def listar():
    """
    Lista as NTs da página de listagem do e-NatJus, com campos já separados.
    Por padrão devolve só as 'Aguardando análise' (as que precisam de parecer).
    Parâmetros opcionais:
      ?todos=1  -> devolve também as já emitidas
      ?debug=1  -> inclui as células brutas de cada linha
    """
    apenas_pendentes = request.args.get("todos") != "1"
    debug = request.args.get("debug") == "1"

    re_nt        = re.compile(r"\b(\d{6})\b")
    re_processo  = re.compile(r"\b(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})\b")
    re_data_hora = re.compile(r"\b(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\b")

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page    = context.new_page()
        try:
            n_cookies = _inject_cookies(context)
            if n_cookies == 0:
                browser.close()
                return jsonify({"erro": "Nenhum cookie configurado"}), 500

            page.goto(LISTA_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)

            if not _check_logged_in(page):
                sc = _screenshot_b64(page)
                browser.close()
                return jsonify({"erro": "Sessão expirada ou cookies inválidos",
                                "screenshot": sc}), 401

            linhas = page.locator("table tr")
            total  = linhas.count()

            nts = []
            for i in range(total):
                try:
                    texto = linhas.nth(i).inner_text().strip()
                except Exception:
                    continue
                if not texto:
                    continue

                celulas = [c.strip() for c in re.split(r"[\t\n]+", texto) if c.strip()]

                m_nt = re_nt.search(texto)
                if not m_nt:
                    continue

                numero_nt    = m_nt.group(1)
                m_proc       = re_processo.search(texto)
                m_data       = re_data_hora.search(texto)
                numero_proc  = m_proc.group(1) if m_proc else ""
                data_solic   = m_data.group(1) if m_data else ""

                if "Nota T" in texto and "emitida" in texto:
                    status_site = "Nota Técnica emitida"
                elif "Aguardando" in texto:
                    status_site = "Aguardando análise"
                else:
                    status_site = ""

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
                    "paciente": paciente,
                    "numero_processo": numero_proc,
                    "vara": vara,
                    "status_site": status_site,
                    "doenca_rara": doenca_rara,
                }
                if debug:
                    registro["celulas"] = celulas

                if apenas_pendentes and status_site != "Aguardando análise":
                    continue

                nts.append(registro)

            browser.close()
            return jsonify({"total_nts": len(nts), "nts": nts})

        except Exception as e:
            sc = _screenshot_b64(page)
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "screenshot": sc}), 500


# ─────────────────────────────────────────────
# Rota [FASE 2] — preenche o formulário da NT (sem submeter)
# ─────────────────────────────────────────────

@app.route("/preencher", methods=["POST"])
def preencher():
    """
    Preenche o formulário da NT no e-NatJus sem submeter.
    [FASE 2 — seletores precisam ser calibrados antes do uso real]
    """
    dados = request.json
    nt = dados.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    log = []
    screenshot_final = None

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context()
        page    = context.new_page()

        try:
            n_cookies = _inject_cookies(context)
            if n_cookies == 0:
                browser.close()
                return jsonify({"erro": "Nenhum cookie configurado."}), 500

            formulario = _navegar_ate_formulario(context, page, nt)
            log.append("Formulário localizado")

            if dados.get("cid"):
                ok = _preencher_campo(formulario, "input[name*='cid'], #cid, input[placeholder*='CID']", dados["cid"])
                log.append(f"CID: {'OK' if ok else 'FALHOU'}")
            if dados.get("diagnostico"):
                ok = _preencher_campo(formulario, "textarea[name*='diagnostico'], #diagnostico", dados["diagnostico"])
                log.append(f"Diagnóstico: {'OK' if ok else 'FALHOU'}")
            if dados.get("meios_confirmatorios"):
                ok = _preencher_campo(formulario, "textarea[name*='meios'], textarea[name*='confirmatorio']", dados["meios_confirmatorios"])
                log.append(f"Meios confirmatórios: {'OK' if ok else 'FALHOU'}")
            if dados.get("tipo_tecnologia"):
                ok = _selecionar_opcao(formulario, "select[name*='tipo'], #tipo_tecnologia", dados["tipo_tecnologia"])
                log.append(f"Tipo de tecnologia: {'OK' if ok else 'FALHOU'}")
            if dados.get("outras_tecnologias"):
                ok = _preencher_campo(formulario, "textarea[name*='outras_tecnologias'], textarea[name*='alternativas']", dados["outras_tecnologias"])
                log.append(f"Outras tecnologias: {'OK' if ok else 'FALHOU'}")
            if dados.get("custo_tecnologia"):
                ok = _preencher_campo(formulario, "textarea[name*='custo']", dados["custo_tecnologia"])
                log.append(f"Custo: {'OK' if ok else 'FALHOU'}")
            if dados.get("fonte_custo"):
                ok = _preencher_campo(formulario, "textarea[name*='fonte']", dados["fonte_custo"])
                log.append(f"Fonte do custo: {'OK' if ok else 'FALHOU'}")
            if dados.get("evidencias"):
                ok = _preencher_campo(formulario, "textarea[name*='evidencia'], textarea[name*='eficacia']", dados["evidencias"])
                log.append(f"Evidências: {'OK' if ok else 'FALHOU'}")
            if dados.get("beneficio_esperado"):
                ok = _preencher_campo(formulario, "textarea[name*='beneficio'], textarea[name*='resultado']", dados["beneficio_esperado"])
                log.append(f"Benefício esperado: {'OK' if ok else 'FALHOU'}")
            if dados.get("recomendacao_conitec"):
                ok = _selecionar_opcao(formulario, "select[name*='conitec'], select[name*='recomendacao']", dados["recomendacao_conitec"])
                log.append(f"Recomendação CONITEC: {'OK' if ok else 'FALHOU'}")
            if dados.get("conclusao_favoravel"):
                ok = _selecionar_opcao(formulario, "select[name*='favoravel'], select[name*='conclusao_select']", dados["conclusao_favoravel"])
                log.append(f"Conclusão (favorável/não): {'OK' if ok else 'FALHOU'}")
            if dados.get("conclusao"):
                ok = _preencher_campo(formulario, "textarea[name*='conclusao']", dados["conclusao"])
                log.append(f"Conclusão (texto): {'OK' if ok else 'FALHOU'}")
            if dados.get("ha_evidencias"):
                ok = _selecionar_opcao(formulario, "select[name*='evidencias_select'], select[name*='ha_evidencia']", dados["ha_evidencias"])
                log.append(f"Há evidências: {'OK' if ok else 'FALHOU'}")
            if dados.get("urgencia"):
                ok = _selecionar_opcao(formulario, "select[name*='urgencia']", dados["urgencia"])
                log.append(f"Urgência: {'OK' if ok else 'FALHOU'}")
            if dados.get("referencias"):
                ok = _preencher_campo(formulario, "textarea[name*='referencia'], textarea[name*='bibliograf']", dados["referencias"])
                log.append(f"Referências: {'OK' if ok else 'FALHOU'}")

            natjus = dados.get("natjus_responsavel", "CE")
            ok = _selecionar_opcao(formulario, "select[name*='natjus'], select[name*='responsavel']", natjus)
            log.append(f"NatJus responsável ({natjus}): {'OK' if ok else 'FALHOU'}")

            if dados.get("instituicao_responsavel"):
                ok = _preencher_campo(formulario, "input[name*='instituicao'], #instituicao", dados["instituicao_responsavel"])
                log.append(f"Instituição: {'OK' if ok else 'FALHOU'}")

            tutoria = dados.get("apoio_tutoria", "Não")
            ok = _selecionar_opcao(formulario, "select[name*='tutoria']", tutoria)
            log.append(f"Apoio tutoria ({tutoria}): {'OK' if ok else 'FALHOU'}")

            if dados.get("outras_informacoes"):
                ok = _preencher_campo(formulario, "textarea[name*='outras_info'], textarea[name*='outras_informacoes']", dados["outras_informacoes"])
                log.append(f"Outras informações: {'OK' if ok else 'FALHOU'}")

            screenshot_final = _screenshot_b64(formulario)
            log.append("Formulário preenchido — NÃO submetido (aguardando aprovação manual)")

            browser.close()
            return jsonify({"sucesso": True, "numeroNT": nt, "log": log, "screenshot": screenshot_final})

        except Exception as e:
            sc = _screenshot_b64(page)
            try:
                browser.close()
            except Exception:
                pass
            return jsonify({"erro": str(e), "log": log, "screenshot": sc}), 500


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
