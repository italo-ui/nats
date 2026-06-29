import os, time, base64, json, subprocess
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

ENATJUS_BASE = "https://www.pje.jus.br/e-natjus"
LOGIN_URL     = f"{ENATJUS_BASE}/index.php"
LISTA_URL     = f"{ENATJUS_BASE}/notaTecnica-solicitacao-listar.php"

COOKIES_JSON_ENV = "ENATJUS_COOKIES"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

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
        if "secure" in c:
            pc["secure"] = bool(c["secure"])
        if "httpOnly" in c:
            pc["httpOnly"] = bool(c["httpOnly"])
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
        nav_text = page.inner_text("nav")
        return "Login" not in nav_text
    except Exception:
        return False


def _screenshot_b64(page):
    try:
        return base64.b64encode(page.screenshot(full_page=True)).decode()
    except Exception:
        return None


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
# Rota de diagnóstico — fotografa cada etapa
# ─────────────────────────────────────────────

@app.route("/diagnostico/<nt>", methods=["GET"])
def diagnostico(nt):
    """
    Percorre o fluxo completo e retorna screenshots de cada etapa.
    Use para depurar sem precisar baixar PDFs.
    """
    etapas = []

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        try:
            # 1. Injeta cookies e vai para lista
            _inject_cookies(context)
            page.goto(LISTA_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            etapas.append({
                "etapa": "1_lista_carregada",
                "url": page.url,
                "autenticado": _check_logged_in(page),
                "screenshot": _screenshot_b64(page)
            })

            # 2. Procura a linha com o número da NT
            linha = page.locator(f"tr:has-text('{nt}')").first
            if linha.count() == 0:
                etapas.append({"etapa": "2_nt_nao_encontrada", "nt": nt})
                browser.close()
                return jsonify({"etapas": etapas})

            etapas.append({
                "etapa": "2_linha_encontrada",
                "texto_linha": linha.inner_text(),
                "screenshot": _screenshot_b64(page)
            })

            # 3. Clica no botão "Ações" da linha
            btn_acoes = linha.locator("button, a").filter(has_text="Ações").first
            if btn_acoes.count() == 0:
                # Tenta dropdown genérico
                btn_acoes = linha.locator(".dropdown-toggle, [data-toggle='dropdown']").first

            btn_acoes.click()
            time.sleep(1)
            etapas.append({
                "etapa": "3_acoes_clicado",
                "screenshot": _screenshot_b64(page)
            })

            # 4. Clica em "Nota Técnica" no dropdown
            opcao_nt = page.locator("a:has-text('Nota Técnica'), a:has-text('Nota Tecnica')").first
            
            # Captura nova aba ao clicar
            with context.expect_page() as nova_aba_info:
                opcao_nt.click()
            
            nova_aba = nova_aba_info.value
            nova_aba.wait_for_load_state("networkidle", timeout=30000)
            etapas.append({
                "etapa": "4_nova_aba_aberta",
                "url": nova_aba.url,
                "html_trecho": nova_aba.content()[:3000],
                "screenshot": _screenshot_b64(nova_aba)
            })

        except Exception as e:
            etapas.append({
                "etapa": "ERRO",
                "mensagem": str(e),
                "screenshot": _screenshot_b64(page)
            })

        browser.close()

    return jsonify({"nt": nt, "etapas": etapas})


# ─────────────────────────────────────────────
# Rota principal — baixa os PDFs
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
            # 1. Injeta cookies
            n_cookies = _inject_cookies(context)
            if n_cookies == 0:
                browser.close()
                return jsonify({"erro": "Nenhum cookie configurado. Configure ENATJUS_COOKIES no Railway."}), 500

            # 2. Vai para lista e verifica autenticação
            page.goto(LISTA_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            if not _check_logged_in(page):
                sc = _screenshot_b64(page)
                browser.close()
                return jsonify({
                    "erro": "Sessão expirada ou cookies inválidos. Atualize ENATJUS_COOKIES.",
                    "screenshot": sc
                }), 401

            # 3. Encontra a linha da NT na tabela
            # A tabela pode paginar — tenta achar na página atual primeiro
            linha = page.locator(f"tr:has-text('{nt}')").first
            if linha.count() == 0:
                # Tenta buscar diretamente pela URL de dados
                page.goto(f"{ENATJUS_BASE}/notaTecnica-dados.php?idNotaTecnica={nt}", timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                linha = None  # Sem linha nesse contexto

            # 4. Fluxo via listagem (linha encontrada)
            if linha and linha.count() > 0:
                # Clica em "Ações"
                btn_acoes = linha.locator("button, a").filter(has_text="Ações").first
                if btn_acoes.count() == 0:
                    btn_acoes = linha.locator(".dropdown-toggle, [data-toggle='dropdown']").first
                btn_acoes.click()
                time.sleep(1)

                # Clica em "Nota Técnica" e captura nova aba
                opcao_nt = page.locator("a:has-text('Nota Técnica'), a:has-text('Nota Tecnica')").first
                with context.expect_page() as nova_aba_info:
                    opcao_nt.click()
                pagina_nt = nova_aba_info.value

            else:
                # Fallback: já está na página da NT diretamente
                pagina_nt = page

            pagina_nt.wait_for_load_state("networkidle", timeout=30000)

            # 5. Encontra e baixa os PDFs na página da NT
            seletores_pdf = [
                "a[href*='.pdf']",
                "a[href*='download']",
                "a[href*='arquivo']",
                "a:has-text('PDF')",
                "a:has-text('Download')",
                "a:has-text('Baixar')",
                "button:has-text('PDF')",
                "button:has-text('Download')",
            ]

            links_pdf = []
            vistos = set()
            for sel in seletores_pdf:
                els = pagina_nt.locator(sel)
                for i in range(els.count()):
                    el = els.nth(i)
                    try:
                        href = el.get_attribute("href") or ""
                        texto = el.inner_text().strip()
                        chave = href or texto
                        if chave and chave not in vistos:
                            vistos.add(chave)
                            links_pdf.append(el)
                    except Exception:
                        pass

            if not links_pdf:
                sc = _screenshot_b64(pagina_nt)
                browser.close()
                return jsonify({
                    "erro": "Nenhum link de PDF encontrado na página da Nota Técnica",
                    "url_pagina": pagina_nt.url,
                    "screenshot": sc
                }), 404

            # 6. Baixa cada PDF
            for idx, link in enumerate(links_pdf[:5]):  # máximo 5 PDFs
                try:
                    href = link.get_attribute("href") or ""

                    if href.endswith(".pdf") or "download" in href or "arquivo" in href:
                        # Link direto — baixa via request HTTP com cookies
                        if not href.startswith("http"):
                            href = f"{ENATJUS_BASE}/{href.lstrip('/')}"
                        
                        cookies_dict = {c["name"]: c["value"] for c in context.cookies()}
                        import urllib.request
                        req = urllib.request.Request(href)
                        for nome, valor in cookies_dict.items():
                            req.add_header("Cookie", f"{nome}={valor}")
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            conteudo = resp.read()
                    else:
                        # Clique com captura de download
                        with pagina_nt.expect_download(timeout=30000) as dl_info:
                            link.click()
                        download = dl_info.value
                        path = f"/tmp/nt_{nt}_{idx+1}.pdf"
                        download.save_as(path)
                        with open(path, "rb") as f:
                            conteudo = f.read()

                    if conteudo:
                        pdfs.append({
                            "nome": f"NT_{nt}_arquivo_{idx+1}.pdf",
                            "base64": base64.b64encode(conteudo).decode()
                        })

                except Exception as e:
                    erros.append(f"PDF {idx+1}: {str(e)}")

        except Exception as e:
            sc = _screenshot_b64(page)
            browser.close()
            return jsonify({"erro": str(e), "avisos": erros, "screenshot": sc}), 500

        browser.close()

    if not pdfs:
        return jsonify({"erro": "Nenhum PDF baixado", "detalhes": erros}), 500

    return jsonify({"numeroNT": nt, "total_pdfs": len(pdfs), "pdfs": pdfs, "avisos": erros})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
