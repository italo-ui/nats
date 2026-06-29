import os, time, base64
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/baixar", methods=["POST"])
def baixar():
    # Lê a senha aqui dentro, não no início do arquivo
    SENHA = os.environ.get("ENATJUS_SENHA", "")
    if not SENHA:
        return jsonify({"erro": "Variável ENATJUS_SENHA não configurada no Railway"}), 500

    nt = request.json.get("numeroNT")
    if not nt:
        return jsonify({"erro": "numeroNT obrigatorio"}), 400

    pdfs = []
    erros = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            page.goto("https://www.pje.jus.br/e-natjus/", timeout=30000)
            page.wait_for_load_state("networkidle")
            page.click("text=PDPJ", timeout=10000)
            page.wait_for_load_state("networkidle")
            page.fill("input[name='username']", "83069925391")
            page.fill("input[name='password']", SENHA)
            page.click("button:has-text('Entrar')")
            page.wait_for_load_state("networkidle")

            page.goto(
                f"https://www.pje.jus.br/e-natjus/notaTecnica-dados.php?idNotaTecnica={nt}",
                timeout=30000
            )
            page.wait_for_load_state("networkidle")

            seletores = [
                "a[href*='.pdf']",
                "a:has-text('PDF')",
                "a:has-text('Download')",
                "button:has-text('PDF')"
            ]

            botoes = []
            for sel in seletores:
                els = page.locator(sel)
                for i in range(min(els.count(), 3)):
                    botoes.append(els.nth(i))

            for idx, botao in enumerate(botoes[:3]):
                try:
                    with context.expect_download(timeout=15000) as dl:
                        botao.click()
                    download = dl.value
                    path = f"/tmp/nt_{nt}_{idx+1}.pdf"
                    download.save_as(path)
                    with open(path, "rb") as f:
                        pdfs.append({
                            "nome": f"NT_{nt}_arquivo_{idx+1}.pdf",
                            "base64": base64.b64encode(f.read()).decode()
                        })
                    time.sleep(1)
                except Exception as e:
                    erros.append(f"PDF {idx+1}: {str(e)}")

        except Exception as e:
            browser.close()
            return jsonify({"erro": str(e)}), 500

        browser.close()

    if not pdfs:
        return jsonify({"erro": "Nenhum PDF baixado", "detalhes": erros}), 500

    return jsonify({"numeroNT": nt, "pdfs": pdfs, "avisos": erros})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
