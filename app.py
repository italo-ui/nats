import os, time, base64, json, subprocess
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

ENATJUS_BASE = "https://www.pje.jus.br/e-natjus"
LOGIN_URL     = f"{ENATJUS_BASE}/index.php"
LISTA_URL     = f"{ENATJUS_BASE}/notaTecnica-solicitacao-listar.php"

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


def _navegar_ate_pagina_nt(context, page, nt):
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

    # Espera o conteúdo dinâmico carregar — aguarda até 15s por qualquer
    # elemento que indique que a página carregou de verdade
    for seletor_espera in [
        "text=Baixar",
        "text=Anexo",
        "text=anexo",
        ".panel-body",
        "#conteudo table",
        "#conteudo form",
    ]:
        try:
            pagina_nt.wait_for_selector(seletor_espera, timeout=15000)
            break  # achou — para de esperar
        except Exception:
            pass  # tenta o próximo

    return pagina_nt


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
# Diagnóstico — inspeciona a página da NT
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

            screenshot = _screenshot_b64(pagina_nt)

            # Captura o HTML completo da área de conteúdo
            html_conteudo = ""
            try:
                conteudo_div = pagina_nt.locator("#conteudo").first
                if conteudo_div.count() > 0:
                    html_conteudo = conteudo_div.inner_html()[:5000]
                else:
                    html_conteudo = pagina_nt.content()[:5000]
            except Exception as ex:
                html_conteudo = f"Erro ao capturar HTML: {ex}"

            # Busca todos os links e botões da página
            todos_elementos = []
            try:
                # Todos os <a>
                links = pagina_nt.locator("a")
                for i in range(links.count()):
                    try:
                        el = links.nth(i)
                        todos_elementos.append({
                            "tag": "a",
                            "texto": el.inner_text().strip()[:100],
                            "href": el.get_attribute("href") or "",
                            "onclick": el.get_attribute("onclick") or "",
                            "class": el.get_attribute("class") or "",
                        })
                    except Exception:
                        pass

                # Todos os botões
                botoes = pagina_nt.locator("button, input[type='button'], input[type='submit']")
                for i in range(botoes.count()):
                    try:
                        el = botoes.nth(i)
                        todos_elementos.append({
                            "tag": "button",
                            "texto": (el.inner_text().strip() or el.get_attribute("value") or "")[:100],
                            "href": "",
                            "onclick": el.get_attribute("onclick") or "",
                            "class": el.get_attribute("class") or "",
                        })
                    except Exception:
                        pass
            except Exception as ex:
                todos_elementos.append({"erro": str(ex)})

            browser.close()
            return jsonify({
                "nt": nt,
                "url_pagina": pagina_nt.url,
                "total_elementos": len(todos_elementos),
                "elementos": todos_elementos,
                "html_conteudo_5000chars": html_conteudo,
                "screenshot": screenshot
            })

        except Exception as e:
            sc = _screenshot_b64(page)
            browser.close()
            return jsonify({"erro": str(e), "screenshot": sc}), 500


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
            n_cookies = _inject_cookies(context)
            if n_cookies == 0:
                browser.close()
                return jsonify({"erro": "Nenhum cookie configurado."}), 500

            pagina_nt = _navegar_ate_pagina_nt(context, page, nt)

            # Busca botões "Baixar Arquivo"
            seletores_pdf = [
                "a:has-text('Baixar Arquivo')",
                "a:has-text('Baixar arquivo')",
                "button:has-text('Baixar Arquivo')",
                "button:has-text('Baixar arquivo')",
                "a:has-text('Baixar')",
                "a[href*='.pdf']",
                "a[href*='arquivo']",
                "a[href*='download']",
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
                        href    = el.get_attribute("href") or ""
                        onclick = el.get_attribute("onclick") or ""
                        texto   = el.inner_text().strip()
                        chave   = href or onclick or texto
                        if chave and chave not in vistos:
                            vistos.add(chave)
                            links_pdf.append(el)
                    except Exception:
                        pass

            if not links_pdf:
                sc = _screenshot_b64(pagina_nt)
                browser.close()
                return jsonify({
                    "erro": "Nenhum botão de download encontrado",
                    "url_pagina": pagina_nt.url,
                    "screenshot": sc
                }), 404

            for idx, link in enumerate(links_pdf[:5]):
                try:
                    href = link.get_attribute("href") or ""

                    if href and (href.endswith(".pdf") or "download" in href or "arquivo" in href):
                        if not href.startswith("http"):
                            href = f"{ENATJUS_BASE}/{href.lstrip('/')}"

                        import urllib.request
                        cookies_dict = {c["name"]: c["value"] for c in context.cookies()}
                        req = urllib.request.Request(href)
                        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
                        req.add_header("Cookie", cookie_str)
                        req.add_header("User-Agent", "Mozilla/5.0")
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            conteudo = resp.read()
                    else:
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
