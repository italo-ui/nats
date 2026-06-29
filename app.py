import os, time, base64, json, subprocess, urllib.request, urllib.error
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

ENATJUS_BASE = "https://www.pje.jus.br/e-natjus"
LOGIN_URL     = f"{ENATJUS_BASE}/index.php"
LISTA_URL     = f"{ENATJUS_BASE}/notaTecnica-solicitacao-listar.php"
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
    """Monta string de cookies para requisições HTTP."""
    return "; ".join(f"{c['name']}={c['value']}" for c in context.cookies())


def _navegar_ate_pagina_nt(context, page, nt):
    """Navega pela listagem → Ações → Nota Técnica e retorna a nova aba."""
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

    # Espera o conteúdo dinâmico carregar
    for seletor_espera in [
        "input.fileform",
        "text=Baixar arquivo",
        ".dm-uploader",
        "#conteudo form",
        "#conteudo table",
    ]:
        try:
            pagina_nt.wait_for_selector(seletor_espera, timeout=15000)
            break
        except Exception:
            pass

    return pagina_nt


def _extrair_hashes(pagina_nt):
    """
    Extrai os hashes dos inputs ocultos .fileform na seção de anexos.
    Retorna lista de dicts: {hash, nome_arquivo}
    """
    hashes = []
    try:
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

                if hash_val.strip():  # ignora hashes vazios
                    hashes.append({"hash": hash_val.strip(), "nome": nome_val.strip()})
            except Exception:
                pass
    except Exception as e:
        hashes.append({"erro": str(e)})

    return hashes


def _baixar_arquivo(hash_val, cookie_str, nome_sugerido="arquivo"):
    """Baixa um arquivo pelo hash via HTTP com os cookies da sessão."""
    url = DOWNLOAD_URL.format(hash=hash_val)
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie_str)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    req.add_header("Referer", ENATJUS_BASE + "/")
    with urllib.request.urlopen(req, timeout=60) as resp:
        conteudo = resp.read()
        content_type = resp.headers.get("Content-Type", "")
    return conteudo, content_type


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


# ─────────────────────────────────────────────
# Diagnóstico
# ─────────────────────────────────────────────

@app.route("/diagnostico/<nt>", methods=["GET"])
def diagnostico(nt):
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()
        try:
            _inject_cookies(context)
            pagina_nt = _navegar_ate_pagina_nt(context, page, nt)

            hashes = _extrair_hashes(pagina_nt)
            screenshot = _screenshot_b64(pagina_nt)

            browser.close()
            return jsonify({
                "nt": nt,
                "url_pagina": pagina_nt.url,
                "hashes_encontrados": hashes,
                "screenshot": screenshot
            })
        except Exception as e:
            sc = _screenshot_b64(page)
            browser.close()
            return jsonify({"erro": str(e), "screenshot": sc}), 500


# ─────────────────────────────────────────────
# Rota principal — baixa os PDFs pelos hashes
# ─────────────────────────────────────────────

@app.route("/baixar", methods=["POST"])
def baixar():
    nt = request.json.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    pdfs  = []
    erros = []

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        try:
            n_cookies = _inject_cookies(context)
            if n_cookies == 0:
                browser.close()
                return jsonify({"erro": "Nenhum cookie configurado."}), 500

            # Navega até a página da NT
            pagina_nt = _navegar_ate_pagina_nt(context, page, nt)

            # Extrai hashes dos anexos
            hashes = _extrair_hashes(pagina_nt)
            hashes_validos = [h for h in hashes if "hash" in h and h["hash"]]

            if not hashes_validos:
                sc = _screenshot_b64(pagina_nt)
                browser.close()
                return jsonify({
                    "erro": "Nenhum anexo com hash encontrado na NT",
                    "url_pagina": pagina_nt.url,
                    "screenshot": sc
                }), 404

            # Monta cookie string para download HTTP
            cookies = _cookie_str(context)
            browser.close()  # Pode fechar o browser — download é via HTTP direto

            # Baixa cada arquivo
            for idx, info in enumerate(hashes_validos[:5]):
                try:
                    conteudo, content_type = _baixar_arquivo(info["hash"], cookies)

                    # Define extensão pelo content-type ou nome original
                    nome = info.get("nome") or f"NT_{nt}_arquivo_{idx+1}.pdf"
                    if not nome.endswith(".pdf") and "pdf" in content_type:
                        nome = nome + ".pdf" if "." not in nome else nome

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
