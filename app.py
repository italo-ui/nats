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

@app.route("/baixar", methods=["POST"])
def baixar():
    SENHA = os.environ.get("ENATJUS_SENHA", "d14m01@A80")
    if not SENHA:
        return jsonify({"erro": "Variável ENATJUS_SENHA não configurada"}), 500

    nt = request.json.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    print(f"Iniciando download NT: {nt}", flush=True)
    pdfs = []
    erros = []

    with sync_playwright() as p:
        print("Playwright iniciado", flush=True)
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--no-zygote",
                "--disable-setuid-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
                "--memory-pressure-off"
            ]
        )
        print("Browser aberto", flush=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            print("Acessando e-NatJus...", flush=True)
            page.goto("https://www.pje.jus.br/e-natjus/", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Página carregada", flush=True)

            page.click("text=PDPJ", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Clicou em PDPJ", flush=True)

            page.fill("input[name='username']", "83069925391")
            page.fill("input[name='password']", SENHA)
            page.click("button:has-text('Entrar')")
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Login realizado", flush=True)

            url_nt = f"https://www.pje.jus.br/e-natjus/notaTecnica-dados.php?idNotaTecnica={nt}"
            page.goto(url_nt, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print(f"NT {nt} carregada", flush=True)

            seletores = [
                "a[href*='.pdf']",
                "a:has-text('PDF')",
                "a:has-text('Download')",
                "button:has-text('PDF')"
            ]

            botoes = []
            for sel in seletores:
                els = page.locator(sel)
                count = els.count()
                print(f"Seletor '{sel}': {count} elementos", flush=True)
                for i in range(min(count, 3)):
                    botoes.append(els.nth(i))

            print(f"Total de botões PDF encontrados: {len(botoes)}", flush=True)

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
            return jsonify({"erro": str(e)}), 500

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
