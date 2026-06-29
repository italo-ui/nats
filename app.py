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
                "html": page.content()[:3000]
            })

            # Etapa 2: seleciona PDPJ no dropdown
            page.select_option("select[name='modalidade']", value="3")
            print("Selecionou PDPJ", flush=True)

            # Etapa 3: preenche login
            page.fill("input[name='login']", "83069925391")
            page.fill("input[name='senha']", os.environ.get("ENATJUS_SENHA", "d14m01@A80"))

            capturas.append({
                "etapa": "2_campos_preenchidos",
                "url": page.url,
                "screenshot": base64.b64encode(page.screenshot()).decode()
            })

            # Etapa 4: submete o formulário
            page.click("button[type='submit'], input[type='submit'], button:has-text('Entrar'), button:has-text('Login')")
            page.wait_for_load_state("networkidle", timeout=30000)

            capturas.append({
                "etapa": "3_apos_login",
                "url": page.url,
                "html": page.content()[:3000],
                "screenshot": base64.b64encode(page.screenshot()).decode()
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

            # Seleciona PDPJ no dropdown
            page.select_option("select[name='modalidade']", value="3")
            print("PDPJ selecionado", flush=True)

            # Preenche credenciais
            page.fill("input[name='login']", "83069925391")
            page.fill("input[name='senha']", SENHA)
            print("Credenciais preenchidas", flush=True)

            # Submete o formulário
            page.click("button[type='submit'], input[type='submit'], button:has-text('Entrar')")
            page.wait_for_load_state("networkidle", timeout=30000)
            print(f"Login realizado. URL atual: {page.url}", flush=True)

            # Navega direto para a NT
            url_nt = f"https://www.pje.jus.br/e-natjus/notaTecnica-dados.php?idNotaTecnica={nt}"
            page.goto(url_nt, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print(f"NT {nt} carregada. URL: {page.url}", flush=True)

            # Localiza botões de PDF
            seletores_pdf = [
                "a[href*='.pdf']",
                "a[href*='download']",
                "a:has-text('PDF')",
                "a:has-text('Download')",
                "button:has-text('PDF')",
                "button:has-text('Download')",
                "input[type='button'][value*='PDF']",
                "input[type='submit'][value*='PDF']",
            ]

            botoes = []
            for sel in seletores_pdf:
                els = page.locator(sel)
                count = els.count()
                print(f"Seletor PDF '{sel}': {count} elementos", flush=True)
                for i in range(min(count, 3)):
                    botoes.append(els.nth(i))

            print(f"Total de botões PDF: {len(botoes)}", flush=True)

            for idx, botao in enumerate(botoes[:3]):
                try:
                    print(f"Baixando PDF {idx+1}...", flush=True)
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
                    print(f"PDF {idx+1} baixado: {len(conteudo)} bytes", flush=True)
                    time.sleep(2)
                except Exception as e:
                    print(f"Erro PDF {idx+1}: {e}", flush=True)
                    erros.append(f"PDF {idx+1}: {str(e)}")

        except Exception as e:
            print(f"Erro geral: {e}", flush=True)
            browser.close()
            return jsonify({"erro": str(e), "avisos": erros}), 500

        browser.close()
        print("Browser fechado", flush=True)

    if not pdfs:
        return jsonify({"erro": "Nenhum PDF baixado", "detalhes": erros}), 500

    print(f"Concluído: {len(pdfs)} PDFs baixados", flush=True)
    return jsonify({"numeroNT": nt, "pdfs": pdfs, "avisos": erros})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Iniciando servidor na porta {port}", flush=True)
    app.run(host="0.0.0.0", port=port)
