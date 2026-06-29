import os, time, base64, subprocess
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/teste", methods=["GET"])
def teste():
    try:
        result = subprocess.run(
            ["python", "-c", "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage','--single-process']); b.close(); p.stop(); print('OK')"],
            capture_output=True, text=True, timeout=60
        )
        return jsonify({
            "playwright": "OK" if result.returncode == 0 else "ERRO",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()
        })
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/screenshot", methods=["GET"])
def screenshot():
    capturas = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process","--no-zygote"]
        )
        page = browser.new_page()
        try:
            # Etapa 1: carrega a página
            page.goto("https://www.pje.jus.br/e-natjus/index.php", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            capturas.append({
                "etapa": "1_pagina_inicial",
                "url": page.url,
                "html": page.content()[:2000]
            })

            # Etapa 2: seleciona PDPJ e preenche credenciais
            page.select_option("select[name='modalidade']", value="3")
            page.fill("input[name='login']", "83069925391")
            page.fill("input[name='senha']", os.environ.get("ENATJUS_SENHA", "d14m01@A80"))

            capturas.append({
                "etapa": "2_campos_preenchidos",
                "url": page.url,
                "screenshot": base64.b64encode(page.screenshot()).decode()
            })

            # Etapa 3: submete o formulário e aguarda
            page.click("button[type='submit'], input[type='submit'], button:has-text('Entrar'), button:has-text('Login')")
            
            # Aguarda o modal ou redirect do PDPJ (Keycloak)
            time.sleep(5)
            page.wait_for_load_state("networkidle", timeout=30000)

            capturas.append({
                "etapa": "3_apos_submit",
                "url": page.url,
                "html": page.content()[:3000],
                "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
            })

            # Aguarda possível redirect para Keycloak e login
            time.sleep(3)
            page.wait_for_load_state("networkidle", timeout=30000)

            capturas.append({
                "etapa": "4_apos_espera",
                "url": page.url,
                "html": page.content()[:3000],
                "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
            })

        except Exception as e:
            capturas.append({
                "etapa": "erro",
                "mensagem": str(e),
                "url": page.url,
                "html": page.content()[:2000]
            })
        finally:
            browser.close()

    return jsonify({"capturas": capturas})

@app.route("/baixar", methods=["POST"])
def baixar():
    SENHA = os.environ.get("ENATJUS_SENHA", "d14m01@A80")

    nt = request.json.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    print(f"Iniciando download NT: {nt}", flush=True)
    pdfs = []
    erros = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--single-process","--no-zygote","--disable-setuid-sandbox",
                "--disable-extensions","--memory-pressure-off"
            ]
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            print("Acessando e-NatJus...", flush=True)
            page.goto("https://www.pje.jus.br/e-natjus/index.php", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Página carregada", flush=True)

            # Seleciona PDPJ e preenche credenciais
            page.select_option("select[name='modalidade']", value="3")
            page.fill("input[name='login']", "83069925391")
            page.fill("input[name='senha']", SENHA)
