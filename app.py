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
# INTELIGÊNCIA ARTIFICIAL - AVALIAÇÃO EM LOTE (BULK)
# =========================================================================
def ai_analisar_lote(dados_raspados):
    """
    Recebe um JSON com todos os produtos e todos os cenários possíveis extraídos do portal.
    O Gemini atua como analista fiscal: escolhe o cenário correto cruzando a descrição e extrai as regras.
    """
    prompt = f"""
    Você é um analista fiscal experiente. Analise o seguinte lote JSON de produtos e seus cenários de tributação extraídos do portal.
    
    Para cada produto no lote, execute a seguinte lógica:
    1. Compare o nome do "produto" com as "descricao_opcao_portal" de cada cenário disponível. Identifique qual cenário faz mais sentido.
    2. Usando APENAS o cenário escolhido, leia o 'texto_icms_bruto'.
       - Se o texto indicar que se aplica ICMS-ST, resuma a regra.
       - Se o texto indicar isenção (ex: "não está sujeita ao regime"), responda EXATAMENTE: "Fora da Regra".
       - Identifique o código CEST (7 dígitos numéricos). Se não houver, responda "N/A".
    3. Usando APENAS o cenário escolhido, leia o 'texto_pis_bruto'.
       - Se mencionar tributação "monofásica" ou "monofásico", retorne o texto correspondente.
       - Caso contrário, responda EXATAMENTE: "Fora da Regra".
    
    Devolva ESTRITAMENTE um array JSON nativo neste exato formato (sem marcações markdown, apenas o JSON puro):
    [
      {{
        "id_linha": <mesmo id_linha recebido>,
        "icms": "<resumo da regra ou Fora da Regra>",
        "cest": "<código numérico ou N/A>",
        "pis": "<texto pis ou Fora da Regra>"
      }}
    ]
    
    DADOS RASPADOS:
    {json.dumps(dados_raspados, ensure_ascii=False)}
    """
    
    for tentativa in range(3):
        try:
            # Enforça a saída em JSON nativo para evitar alucinações de formatação
            res = model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            ).text.strip()
            
            # Limpeza de segurança caso a IA mande texto antes do JSON
            match = re.search(r'\[.*\]', res, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            else:
                raise ValueError("JSON não encontrado na resposta.")
                
        except Exception as e:
            time.sleep(4)
            
    return [] # Retorna vazio se falhar todas as tentativas

# =========================================================================
# O MOTOR CORE - RASPAGEM CEGA
# =========================================================================
def raspar_cenarios_ncm(ncm_bruta, descricao_produto, uf_codigo, index, session):
    """Raspa todas as possibilidades de uma NCM no portal, sem tomar decisão."""
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
            return index, {"erro": "NCM não encontrada"}
            
        opcoes_tributacao = {}
        radios = form_alvo.find_all("input", attrs={"name": "tributacao_cod", "type": "radio"})
        
        if radios:
            for radio in radios:
                cod = radio["value"]
                parent_tr = radio.find_parent("tr")
                opcoes_tributacao[cod] = parent_tr.get_text(separator=" ", strip=True) if parent_tr else "Opção"
        else:
            hidden = form_alvo.find("input", attrs={"name": "tributacao_cod", "type": "hidden"})
            if hidden:
                opcoes_tributacao[hidden["value"]] = "Opção Única"
            else:
                return index, {"erro": "NCM não encontrada"}
        
        cenarios_extraidos = []
        
        # === NAVEGA EM TODAS AS OPÇÕES (RASPAGEM TOTAL) ===
        for cod_tributacao, desc_opcao in opcoes_tributacao.items():
            
            # Aciona o Passo 2 para este código específico
            payload_2 = {
                "uf": uf_codigo,
                "estado": "",
                "pesquisa": ncm_formatada,
                "tributacao_cod": cod_tributacao,
                "passo": "2",
                "local": "1",
                "posicao_tipi": "1",
                "descricao": ""
            }
            session.post(url_base, data=payload_2, timeout=15) 
            
            # Extrai ST (Aba 2)
            url_icms_st = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=2&passo=2"
            res_icms = session.get(url_icms_st, timeout=15)
            soup_icms = BeautifulSoup(res_icms.text, "html.parser")
            painel_icms = soup_icms.find("div", class_="panel-primary")
            texto_icms_st = painel_icms.get_text(separator=' ', strip=True) if painel_icms else ""
            
            # Extrai PIS/COFINS (Aba 3)
            url_pis = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=3&passo=2"
            res_pis = session.get(url_pis, timeout=15)
            soup_pis = BeautifulSoup(res_pis.text, "html.parser")
            painel_pis = soup_pis.find("div", class_="panel-primary")
            texto_pis = painel_pis.get_text(separator=' ', strip=True) if painel_pis else ""
            
            cenarios_extraidos.append({
                "codigo": cod_tributacao,
                "descricao_opcao_portal": desc_opcao,
                "texto_icms_bruto": texto_icms_st,
                "texto_pis_bruto": texto_pis
            })
            
        return index, {
            "id_linha": index,
            "produto": descricao_produto,
            "ncm": ncm_formatada,
            "cenarios": cenarios_extraidos
        }

    except Exception as e:
        return index, {"erro": f"Erro Script: {str(e)}"}

def processar_ncm_fila(ncm_bruta, descricao_produto, uf_codigo, index):
    sessao_ativa = sessao_queue.get() 
    try:
        return raspar_cenarios_ncm(ncm_bruta, descricao_produto, uf_codigo, index, sessao_ativa)
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

st.title("⚡ Robô Fiscal - Tributação com IA (Processamento em Lote)")

if not st.session_state.processado:
    
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
            
            if "ICMS_ST" not in df.columns: df["ICMS_ST"] = ""
            if "PIS_COFINS" not in df.columns: df["PIS_COFINS"] = ""
            if "CEST" not in df.columns: df["CEST"] = ""
                
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
                            "User-Agent": "Mozilla/5.0",
                            "Referer": "https://itcnet.com.br/acesso.php?modulo=orientador_fiscal"
                        })
                        sessao_queue.put(sessao_http)
                    
                    browser.close()
                
                status_text.success("🔥 Todos os acessos aprovados! Iniciando extração de dados no portal...")
            except Exception as e:
                status_text.error(f"Erro na criação dos acessos. Detalhe: {e}")
                st.stop()

            # FASE 1: RASPAGEM
            lote_para_ia = []
            erros_raspagem = {}
            concluidos = 0
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futuros = []
                for index, row in df.iterrows():
                    ncm_val = row["NCM"]
                    desc_val = str(row["Descricao"])
                    futuros.append(executor.submit(processar_ncm_fila, ncm_val, desc_val, uf_selecionada, index))
                    
                for futuro in concurrent.futures.as_completed(futuros):
                    idx, resultado = futuro.result()
                    
                    if "erro" in resultado:
                        erros_raspagem[idx] = resultado["erro"]
                    else:
                        lote_para_ia.append(resultado)
                        
                    concluidos += 1
                    progress_bar.progress(int((concluidos / total_linhas) * 100))
                    status_text.text(f"Raspando portal: {concluidos} de {total_linhas} NCMs extraídas...")

            # FASE 2: AVALIAÇÃO DA IA EM LOTE
            if lote_para_ia:
                status_text.info("🧠 Raspagem concluída. Enviando lote completo para a Inteligência Artificial avaliar (isso leva alguns segundos)...")
                resultados_ia = ai_analisar_lote(lote_para_ia)
                
                # Preenche a planilha com o retorno da IA
                for item in resultados_ia:
                    idx = item.get("id_linha")
                    if idx is not None and idx in df.index:
                        df.at[idx, "ICMS_ST"] = item.get("icms", "Erro IA")
                        df.at[idx, "CEST"] = item.get("cest", "Erro IA")
                        df.at[idx, "PIS_COFINS"] = item.get("pis", "Erro IA")
            
            # Preenche as linhas que deram erro logo na raspagem (NCM não encontrada, etc)
            for idx, msg_erro in erros_raspagem.items():
                df.at[idx, "ICMS_ST"] = msg_erro
                df.at[idx, "CEST"] = "N/A"
                df.at[idx, "PIS_COFINS"] = msg_erro
            
            status_text.success("✅ Relatório consolidado e avaliado com sucesso!")
            
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
            file_name="analise_ncm_ia_lote.xlsx",
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
