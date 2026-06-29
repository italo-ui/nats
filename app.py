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
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--no-zygote"
            ]
        )
        page = browser.new_page()
        try:
            # Etapa 1: página inicial
            page.goto("https://www.pje.jus.br/e-natjus/index.php", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            capturas.append({
                "etapa": "1_pagina_inicial",
                "url": page.url,
                "html": page.content()[:5000],
                "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
            })

            # Etapa 2: clicar no botão PDPJ
            seletores = [
                "text=PDPJ",
                "text=Marketplace",
                "a:has-text('PDPJ')",
                "button:has-text('PDPJ')",
                "a:has-text('Marketplace')",
                "[href*='pdpj']",
                "[href*='sso']",
                "[href*='keycloak']",
                "img[alt*='PDPJ']",
                ".btn-login",
                "a.btn",
                "button.btn"
            ]
            clicou = False
            seletor_usado = ""
            for sel in seletores:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        el.click(timeout=5000)
                        clicou = True
                        seletor_usado = sel
                        break
                except:
                    continue

            page.wait_for_load_state("networkidle", timeout=30000)
            capturas.append({
                "etapa": "2_apos_clicar_pdpj",
                "clicou": clicou,
                "seletor_usado": seletor_usado,
                "url": page.url,
                "html": page.content()[:5000],
                "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
            })

            if clicou:
                # Etapa 3: preencher login
                page.fill("input[name='username']", "83069925391")
                page.fill("input[name='password']", os.environ.get("ENATJUS_SENHA", "d14m01@A80"))

                capturas.append({
                    "etapa": "3_campos_preenchidos",
                    "url": page.url,
                    "html": page.content()[:5000],
                    "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
                })

                # Etapa 4: clicar em Entrar
                page.click("input[type='submit'], button[type='submit'], button:has-text('Entrar'), button:has-text('Login')")
                page.wait_for_load_state("networkidle", timeout=30000)

                capturas.append({
                    "etapa": "4_apos_login",
                    "url": page.url,
                    "html": page.content()[:5000],
                    "screenshot": base64.b64encode(page.screenshot(full_page=True)).decode()
                })

        except Exception as e:
            capturas.append({
                "etapa": "erro",
                "mensagem": str(e),
                "url": page.url,
                "html": page.content()[:3000]
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
            page.goto("https://www.pje.jus.br/e-natjus/index.php", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print("Página carregada", flush=True)

            # Tenta clicar no botão PDPJ
            seletores_pdpj = [
                "text=PDPJ",
                "text=Marketplace",
                "a:has-text('PDPJ')",
                "button:has-text('PDPJ')",
                "a:has-text('Marketplace')",
                "[href*='pdpj']",
                "[href*='sso']",
                "[href*='keycloak']",
                "img[alt*='PDPJ']",
                ".btn-login",
                "a.btn",
                "button.btn"
            ]

            clicou = False
            for sel in seletores_pdpj:
                try:
                    elemento = page.locator(sel).first
                    if elemento.count() > 0:
                        elemento.click(timeout=5000)
                        print(f"Clicou em PDPJ usando seletor: {sel}", flush=True)
                        clicou = True
                        break
                except Exception as e:
                    print(f"Seletor '{sel}' falhou: {e}", flush=True)
                    continue

            if not clicou:
                html = page.content()
                print(f"HTML da página (primeiros 2000 chars): {html[:2000]}", flush=True)
                raise Exception("Botão PDPJ não encontrado na página")

            page.wait_for_load_state("networkidle", timeout=30000)
            print("Página de login PDPJ carregada", flush=True)

            # Preenche credenciais
            seletores_usuario = [
                "input[name='username']",
                "input[name='user']",
                "input[name='login']",
                "input[type='text']",
                "input[id*='user']",
                "input[id*='login']",
                "#username",
            ]

            preencheu_usuario = False
            for sel in seletores_usuario:
                try:
                    campo = page.locator(sel).first
                    if campo.count() > 0:
                        campo.fill("83069925391")
                        print(f"Usuário preenchido com seletor: {sel}", flush=True)
                        preencheu_usuario = True
                        break
                except:
                    continue

            if not preencheu_usuario:
                raise Exception("Campo de usuário não encontrado")

            seletores_senha = [
                "input[name='password']",
                "input[name='senha']",
                "input[type='password']",
                "input[id*='pass']",
                "input[id*='senha']",
                "#password",
            ]

            preencheu_senha = False
            for sel in seletores_senha:
                try:
                    campo = page.locator(sel).first
                    if campo.count() > 0:
                        campo.fill(SENHA)
                        print(f"Senha preenchida com seletor: {sel}", flush=True)
                        preencheu_senha = True
                        break
                except:
                    continue

            if not preencheu_senha:
                raise Exception("Campo de senha não encontrado")

            seletores_entrar = [
                "button:has-text('Entrar')",
                "input[type='submit']",
                "button[type='submit']",
                "button:has-text('Login')",
                "button:has-text('Acessar')",
                "[value='Entrar']",
            ]

            clicou_entrar = False
            for sel in seletores_entrar:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0:
                        btn.click(timeout=5000)
                        print(f"Botão Entrar clicado com seletor: {sel}", flush=True)
                        clicou_entrar = True
                        break
                except:
                    continue

            if not clicou_entrar:
                raise Exception("Botão Entrar não encontrado")

            page.wait_for_load_state("networkidle", timeout=30000)
            print("Login realizado", flush=True)

            # Navega para a NT
            url_nt = f"https://www.pje.jus.br/e-natjus/notaTecnica-dados.php?idNotaTecnica={nt}"
            page.goto(url_nt, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            print(f"NT {nt} carregada", flush=True)

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

            # Remove duplicatas mantendo ordem
            vistos = set()
            botoes_unicos = []
            for b in botoes:
                try:
                    txt = b.inner_text()
                    href = b.get_attribute("href") or ""
                    chave = txt + href
                    if chave not in vistos:
                        vistos.add(chave)
                        botoes_unicos.append(b)
                except:
                    botoes_unicos.append(b)

            print(f"Total de botões PDF únicos: {len(botoes_unicos)}", flush=True)

            for idx, botao in enumerate(botoes_unicos[:3]):
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
            try:
                img_erro = page.screenshot()
                print(f"Screenshot do erro: {len(img_erro)} bytes", flush=True)
            except:
                pass
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
