import pandas as pd
import requests
import concurrent.futures
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import streamlit as st
import io
import os
import queue
import re
import time
import json
import google.generativeai as genai

# TRUQUE PARA A NUVEM: Força a instalação do navegador invisível no servidor do Streamlit
os.system("playwright install chromium")

USUARIO_ITC = st.secrets["USUARIO_ITC"]
SENHA_ITC = st.secrets["SENHA_ITC"]

try:
    genai.configure(api_key=st.secrets["gemini_api_key"])
    model = genai.GenerativeModel('gemini-3.5-flash-lite')
except Exception:
    st.error("Chave da API do Gemini não configurada.")

# === 5 NAVEGADORES SIMULTÂNEOS ===
MAX_WORKERS = 5 
sessao_queue = queue.Queue() 

# =========================================================================
# FUNÇÕES DE INTELIGÊNCIA ARTIFICIAL (TOMADA DE DECISÃO)
# =========================================================================
def ai_escolher_tributacao(descricao_produto, opcoes_tributacao):
    """Lê as opções de enquadramento do ITC Net e escolhe a que mais bate com o produto."""
    texto_opcoes = "\n".join([f"Código {cod}: {desc}" for cod, desc in opcoes_tributacao.items()])
    prompt = f"""
    Temos o seguinte produto: "{descricao_produto}"
    
    No portal tributário, essa NCM possui os seguintes desdobramentos/enquadramentos:
    {texto_opcoes}
    
    Qual desses códigos se adequa melhor ao produto? 
    Responda APENAS com o NÚMERO do código escolhido.
    """
    for tentativa in range(3):
        try:
            res = model.generate_content(prompt).text.strip()
            cod = re.search(r'\d+', res).group()
            return cod if cod in opcoes_tributacao else list(opcoes_tributacao.keys())[0]
        except Exception:
            time.sleep(3) 
            
    return list(opcoes_tributacao.keys())[0]

def ai_analisar_st(descricao_produto, ncm, texto_st):
    """Lê o painel de ICMS-ST, verifica se aplica e extrai o CEST em JSON."""
    prompt = f"""
    Produto do cliente: "{descricao_produto}" (NCM: {ncm})
    
    Texto sobre ICMS/ST retornado pelo portal:
    {texto_st}
    
    Analise rigorosamente as regras e exceções descritas no texto.
    1. Este produto específico está sujeito à ST segundo este texto? Se SIM, resuma a regra. Se NÃO (ex: for uma exceção descrita), responda EXATAMENTE: "Fora da Regra".
    2. Identifique o código CEST (7 dígitos numéricos) mencionado no texto. Se não houver, responda "N/A".
    
    Responda APENAS um JSON válido no formato abaixo:
    {{
        "icms": "resumo da regra ou Fora da Regra",
        "cest": "código numérico ou N/A"
    }}
    """
    erro_real = ""
    for tentativa in range(3):
        try:
            # Força o Gemini a devolver JSON nativo para não bugar o Python
            res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"}).text.strip()
            
            # Pinça de segurança para capturar só o bloco JSON
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                dados = json.loads(match.group(0))
                return dados.get("icms", "Fora da Regra"), dados.get("cest", "N/A")
            else:
                return "Erro de formato da IA", "Erro"
                
        except Exception as e:
            erro_real = str(e)
            time.sleep(3) 
            
    return f"Erro na IA: {erro_real}", "Erro"

# =========================================================================
# O MOTOR CORE
# =========================================================================
def processar_ncm_core(ncm_bruta, descricao_produto, uf_codigo, index, session):
    ncm_bruta = str(ncm_bruta).strip()
    ncm_numeros = ncm_bruta.replace(".", "")
    if len(ncm_numeros) == 8:
        ncm_formatada = f"{ncm_numeros[:4]}.{ncm_numeros[4:6]}.{ncm_numeros[6:]}"
    else:
        ncm_formatada = ncm_bruta
        
    url_base = "https://itcnet.com.br/orientador_fiscal/index.php"
    
    try:
        # === PASSO 1: Dispara a Pesquisa ===
        payload_1 = {
            "uf": uf_codigo,
            "pesquisa": ncm_formatada, 
            "passo": "1",
            "local": "1"
        }
        res_passo1 = session.post(url_base, data=payload_1, timeout=15)
        soup_1 = BeautifulSoup(res_passo1.text, "html.parser")
        
        form_alvo = soup_1.find("form", attrs={"name": "selecionar"})
        if not form_alvo:
            return index, "NCM não encontrada", "NCM não encontrada", "N/A"
            
        opcoes_tributacao = {}
        radios = form_alvo.find_all("input", attrs={"name": "tributacao_cod", "type": "radio"})
        
        if radios:
            for radio in radios:
                cod = radio["value"]
                parent_tr = radio.find_parent("tr")
                texto_opcao = parent_tr.get_text(separator=" ", strip=True) if parent_tr else "Opção"
                opcoes_tributacao[cod] = texto_opcao
        else:
            hidden = form_alvo.find("input", attrs={"name": "tributacao_cod", "type": "hidden"})
            if hidden:
                opcoes_tributacao[hidden["value"]] = "Opção Única"
            else:
                return index, "NCM não encontrada", "NCM não encontrada", "N/A"
        
        # === A IA TOMA A DECISÃO DE QUAL ROTA SEGUIR ===
        if len(opcoes_tributacao) > 1:
            tributacao_cod = ai_escolher_tributacao(descricao_produto, opcoes_tributacao)
        else:
            tributacao_cod = list(opcoes_tributacao.keys())[0]
        
        # === PASSO 2: Prosseguir com o código escolhido ===
        payload_2 = {
            "uf": uf_codigo,
            "estado": "",
            "pesquisa": ncm_formatada,
            "tributacao_cod": tributacao_cod,
            "passo": "2",
            "local": "1",
            "posicao_tipi": "1",
            "descricao": ""
        }
        session.post(url_base, data=payload_2, timeout=15) 
        
        # === PASSO 3: ICMS/ST E EXTRAÇÃO DO CEST ===
        url_icms_st = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=2&passo=2"
        res_icms = session.get(url_icms_st, timeout=15)
        soup_icms = BeautifulSoup(res_icms.text, "html.parser")
        
        painel_icms = soup_icms.find("div", class_="panel-primary")
        texto_icms_st = painel_icms.get_text(separator=' ', strip=True) if painel_icms else ""
        
        texto_icms_min = texto_icms_st.lower()
        texto_icms_limpo = " ".join(texto_icms_min.split())
        frase_isencao = "não está sujeita ao regime de substituição tributária"
        
        if (frase_isencao not in texto_icms_limpo) and (len(texto_icms_limpo) > 15):
            # A IA retorna duas variáveis agora: ICMS e CEST
            icms_salvar, cest_salvar = ai_analisar_st(descricao_produto, ncm_formatada, texto_icms_st)
        else:
            icms_salvar, cest_salvar = "Fora da Regra", "N/A"
        
        # === PASSO 4: PIS/COFINS ===
        url_pis = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=3&passo=2"
        res_pis = session.get(url_pis, timeout=15)
        soup_pis = BeautifulSoup(res_pis.text, "html.parser")
        
        painel_pis = soup_pis.find("div", class_="panel-primary")
        texto_pis = painel_pis.get_text(separator=' ', strip=True) if painel_pis else ""
        
        termos_monofasico = ["monofásica", "monofasica", "monofásico", "monofasico"]
        tem_monofasico = any(termo in texto_pis.lower() for termo in termos_monofasico)
        pis_salvar = texto_pis if tem_monofasico else "Fora da Regra"
        
        # O retorno agora manda 4 informações para fechar a planilha
        return index, icms_salvar, pis_salvar, cest_salvar

    except Exception as e:
        return index, f"Erro Script: {str(e)}", f"Erro Script: {str(e)}", "Erro"

def processar_ncm_fila(ncm_bruta, descricao_produto, uf_codigo, index):
    sessao_ativa = sessao_queue.get() 
    try:
        return processar_ncm_core(ncm_bruta, descricao_produto, uf_codigo, index, sessao_ativa)
    finally:
        sessao_queue.put(sessao_ativa) 


# =========================================================================
# INTERFACE STREAMLIT
# =========================================================================
st.set_page_config(page_title="Validador NCM Inteligente", page_icon="⚡", layout="wide")

if "processado" not in st.session_state:
    st.session_state.processado = False
    st.session_state.df_resultado = None
    st.session_state.planilha_bytes = None

st.title("⚡ Robô Fiscal - Tributação com IA")

if not st.session_state.processado:
    
    # 1. Menu de Seleção de Estado
    estados_dict = {
        "1": "Santa Catarina - Nova versão",
        "28": "Santa Catarina",
        "2": "Rio Grande do Sul",
        "3": "Paraná",
        "4": "São Paulo",
        "5": "Minas Gerais",
        "6": "Rio de Janeiro",
        "9": "Espírito Santo - Nova versão"
    }
    
    uf_selecionada = st.selectbox(
        "Selecione o Estado (UF) para consulta:", 
        options=list(estados_dict.keys()), 
        format_func=lambda x: estados_dict[x],
        index=1 
    )
    
    st.markdown("---")
    
    # === CRIAÇÃO E DOWNLOAD DA PLANILHA MODELO ===
    st.subheader("1. Baixe a Planilha Modelo")
    st.markdown("Para que a IA faça a escolha correta, sua planilha deve conter as colunas **NCM** e **Descricao**.")
    
    df_modelo = pd.DataFrame({'NCM': ['22011000', '22021000'], 'Descricao': ['Agua Mineral 500ml', 'Refrigerante Cola 2L']})
    buffer_modelo = io.BytesIO()
    with pd.ExcelWriter(buffer_modelo, engine='openpyxl') as writer:
        df_modelo.to_excel(writer, index=False)
        
    st.download_button(
        label="⬇️ Baixar Planilha Modelo",
        data=buffer_modelo.getvalue(),
        file_name="modelo_ncm.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="secondary",
        width="stretch"
    )
    
    st.markdown("---")
    st.subheader("2. Suba sua Planilha Preenchida")
    arquivo_up = st.file_uploader("Upload da Planilha (.xlsx)", type=["xlsx"])

    if st.button("Iniciar Varredura 🚀", type="primary", width="stretch"):
        
        if arquivo_up is None:
            st.warning("Por favor, faça o upload da planilha antes de iniciar.")
        else:
            df = pd.read_excel(arquivo_up)
            
            if "NCM" not in df.columns or "Descricao" not in df.columns:
                st.error("A planilha precisa ter obrigatoriamente as colunas 'NCM' e 'Descricao'. Use o modelo acima!")
                st.stop()
            
            df = df.dropna(subset=['NCM'])
            
            # Adiciona a coluna CEST também
            if "ICMS_ST" not in df.columns:
                df["ICMS_ST"] = ""
            if "PIS_COFINS" not in df.columns:
                df["PIS_COFINS"] = ""
            if "CEST" not in df.columns:
                df["CEST"] = ""
                
            total_linhas = len(df)
            
            status_text = st.empty()
            progress_bar = st.progress(0)
            
            try:
                status_text.info(f"🔐 Aquecendo os motores... Criando {MAX_WORKERS} acessos simultâneos.")
                
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    for i in range(MAX_WORKERS):
                        context = browser.new_context()
                        page = context.new_page()
                        
                        page.goto("https://itcnet.com.br/auth/keycloak/login.php")
                        page.fill("input#username", USUARIO_ITC)
                        page.fill("input#password", SENHA_ITC)
                        page.click("input#kc-login")
                        
                        page.wait_for_timeout(4000) 
                        
                        cookies = context.cookies()
                        cookie_dict = {c['name']: c['value'] for c in cookies}
                        context.close()
                        
                        sessao_http = requests.Session()
                        sessao_http.cookies.update(cookie_dict)
                        sessao_http.headers.update({
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                            "Referer": "https://itcnet.com.br/acesso.php?modulo=orientador_fiscal"
                        })
                        sessao_http.get("https://itcnet.com.br/acesso.php?modulo=orientador_fiscal", timeout=15)
                        sessao_queue.put(sessao_http)
                    
                    browser.close()
                
                status_text.success("🔥 Todos os acessos aprovados! A IA assumiu o controle...")
            except Exception as e:
                status_text.error(f"Erro na criação dos acessos. Detalhe: {e}")
                st.stop()

            resultados = {}
            concluidos = 0
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futuros = []
                for index, row in df.iterrows():
                    ncm_val = row["NCM"]
                    desc_val = str(row["Descricao"])
                    
                    futuro = executor.submit(processar_ncm_fila, ncm_val, desc_val, uf_selecionada, index)
                    futuros.append(futuro)
                    
                # Desempacota o CEST também no recebimento do resultado
                for futuro in concurrent.futures.as_completed(futuros):
                    idx, val_icms, val_pis, val_cest = futuro.result()
                    resultados[idx] = {"icms": val_icms, "pis": val_pis, "cest": val_cest}
                    
                    concluidos += 1
                    progress = int((concluidos / total_linhas) * 100)
                    progress_bar.progress(progress)
                    status_text.text(f"Processando: {concluidos} de {total_linhas} NCMs concluídas...")

            for idx, dados in resultados.items():
                df.at[idx, "ICMS_ST"] = dados["icms"]
                df.at[idx, "PIS_COFINS"] = dados["pis"]
                df.at[idx, "CEST"] = dados["cest"]
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            
            st.session_state.df_resultado = df
            st.session_state.planilha_bytes = output.getvalue()
            st.session_state.processado = True
            
            st.rerun()

else:
    st.success("✅ Varredura inteligente concluída!")
    
    st.dataframe(st.session_state.df_resultado, width="stretch")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.download_button(
            label="📥 Baixar Resultado",
            data=st.session_state.planilha_bytes,
            file_name="analise_ncm_ia.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch"
        )
        
    with col2:
        if st.button("🔄 Nova Consulta", width="stretch"):
            st.session_state.processado = False
            st.session_state.df_resultado = None
            st.session_state.planilha_bytes = None
            st.rerun()
