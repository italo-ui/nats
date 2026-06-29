import os, time, base64, json, subprocess
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

ENATJUS_BASE = "https://www.pje.jus.br/e-natjus"
LOGIN_URL     = f"{ENATJUS_BASE}/index.php"

# Cookies exportados do Chrome (JSON array) ficam nesta variável de ambiente
# Exemplo: '[{"name":"PHPSESSID","value":"abc123","domain":"www.pje.jus.br",...}]'
COOKIES_JSON_ENV = "ENATJUS_COOKIES"

def _launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
              "--single-process","--no-zygote","--disable-setuid-sandbox",
              "--disable-extensions","--memory-pressure-off"]
    )

def _inject_cookies(context):
    """
    Lê os cookies exportados do Chrome da variável de ambiente
    e os injeta no contexto do Playwright.
    """
    raw = os.environ.get(COOKIES_JSON_ENV, "[]")
    cookies = json.loads(raw)

    # Playwright exige campos específicos — normaliza o formato do Cookie-Editor
    playwright_cookies = []
    for c in cookies:
        pc = {
            "name":   c.get("name", ""),
            "value":  c.get("value", ""),
            "domain": c.get("domain", "www.pje.jus.br"),
            "path":   c.get("path", "/"),
        }
        # Campos opcionais
        if "secure" in c:
            pc["secure"] = bool(c["secure"])
        if "httpOnly" in c:
            pc["httpOnly"] = bool(c["httpOnly"])
        if "sameSite" in c:
            # Playwright aceita: "Strict" | "Lax" | "None"
            ss = c["sameSite"]
            if isinstance(ss, str) and ss in ("Strict", "Lax", "None"):
                pc["sameSite"] = ss
        # expires: Cookie-Editor usa número Unix; Playwright aceita float
        if "expirationDate" in c:
            pc["expires"] = float(c["expirationDate"])
        playwright_cookies.append(pc)

    if playwright_cookies:
        context.add_cookies(playwright_cookies)
    return len(playwright_cookies)


def _check_logged_in(page):
    """Verifica se está autenticado: menu não deve ter botão 'Login'."""
    try:
        nav_text = page.inner_text("nav")
        return "Login" not in nav_text
    except Exception:
        return False


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
    """
    Rota de diagnóstico: injeta cookies do Chrome, acessa o e-NatJus
    e verifica se está autenticado.
    """
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
                "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
            }
            browser.close()
            return jsonify(resultado)
        except Exception as e:
            try:
                sc = base64.b64encode(page.screenshot(full_page=True)).decode()
            except Exception:
                sc = None
            browser.close()
            return jsonify({"erro": str(e), "url": page.url, "screenshot": sc}), 500


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
                return jsonify({"erro": "Nenhum cookie configurado. Configure ENATJUS_COOKIES no Railway."}), 500

            # Verifica autenticação antes de continuar
            page.goto(LOGIN_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            if not _check_logged_in(page):
                sc = base64.b64encode(page.screenshot(full_page=True)).decode()
                browser.close()
                return jsonify({
                    "erro": "Sessão expirada ou cookies inválidos. Atualize ENATJUS_COOKIES.",
                    "screenshot": sc
                }), 401

            # Navega para a NT
            url_nt = f"{ENATJUS_BASE}/notaTecnica-dados.php?idNotaTecnica={nt}"
            page.goto(url_nt, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)

            seletores_pdf = [
                "a[href*='.pdf']","a[href*='download']",
                "a:has-text('PDF')","a:has-text('Download')",
                "button:has-text('PDF')","button:has-text('Download')",
                "input[type='button'][value*='PDF']","input[type='submit'][value*='PDF']",
            ]
            botoes = []
            for sel in seletores_pdf:
                els = page.locator(sel)
                for i in range(min(els.count(), 3)):
                    botoes.append(els.nth(i))

            for idx, botao in enumerate(botoes[:3]):
                try:
                    with context.expect_download(timeout=30000) as dl:
                        botao.click()
                    download = dl.value
                    path = f"/tmp/nt_{nt}_{idx+1}.pdf"
                    download.save_as(path)
                    with open(path, "rb") as f:
                        conteudo = f.read()
                    pdfs.append({
                        "nome": f"NT_{nt}_arquivo_{idx+1}.pdf",
                        "base64": base64.b64encode(conteudo).decode()
                    })
                    time.sleep(2)
                except Exception as e:
                    erros.append(f"PDF {idx+1}: {str(e)}")

        except Exception as e:
            browser.close()
            return jsonify({"erro": str(e), "avisos": erros}), 500

        browser.close()

    if not pdfs:
        return jsonify({"erro": "Nenhum PDF baixado", "detalhes": erros}), 500

    return jsonify({"numeroNT": nt, "pdfs": pdfs, "avisos": erros})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
