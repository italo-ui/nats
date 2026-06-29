import os, time, base64, subprocess
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

LOGIN_URL = "https://www.pje.jus.br/e-natjus/index.php"
CPF       = "83069925391"

def _launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
              "--single-process","--no-zygote","--disable-setuid-sandbox",
              "--disable-extensions","--memory-pressure-off"]
    )

def _do_login(page, SENHA):
    """
    Fluxo real:
    1. Abre index.php
    2. loginPDPJ() executa automaticamente e redireciona para SSO PDPJ
    3. Aguarda a página SSO carregar (com CPF pré-preenchido)
    4. Preenche só a senha e submete
    5. Aguarda voltar para o e-NatJus
    """
    # Abre a página — o JS vai redirecionar automaticamente para o SSO
    page.goto(LOGIN_URL, timeout=60000)

    # Aguarda redirect para o SSO (até 45s — pode ser lento)
    page.wait_for_url("**/sso*/**", timeout=45000)
    page.wait_for_load_state("networkidle", timeout=30000)

    # O CPF já vem pré-preenchido — só preenche a senha
    page.fill("input[type='password']", SENHA)

    # Submete
    page.click("input[type='submit'], button[type='submit'], button:has-text('Entrar'), button:has-text('Login')")

    # Aguarda voltar para o e-NatJus
    page.wait_for_url("**/e-natjus/**", timeout=45000)
    page.wait_for_load_state("networkidle", timeout=30000)


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
    """Rota de diagnóstico: testa login completo e retorna URL + screenshot."""
    SENHA = os.environ.get("ENATJUS_SENHA", "")
    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_page()
        try:
            _do_login(page, SENHA)
            resultado = {
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
    SENHA = os.environ.get("ENATJUS_SENHA", "")
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
            _do_login(page, SENHA)

            # Navega para a NT
            url_nt = f"https://www.pje.jus.br/e-natjus/notaTecnica-dados.php?idNotaTecnica={nt}"
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
