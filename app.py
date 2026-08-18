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
import itertools
import google.generativeai as genai
from ncm.client import FetchNcm
from ncm.exceptions import NcmDownloadException

# TRUQUE PARA A NUVEM: Força a instalação do navegador invisível no servidor do Streamlit
os.system("playwright install chromium")

# === CREDENCIAIS DO PORTAL ITC NET (ESTAVAM FALTANDO) ===
USUARIO_ITC = st.secrets["USUARIO_ITC"]
SENHA_ITC = st.secrets["SENHA_ITC"]

# =========================================================================
# CONFIGURAÇÃO DE MÚLTIPLAS CHAVES API (ROTAÇÃO CONTÍNUA)
# =========================================================================
CHAVES_GEMINI = []
for i in range(1, 6):  # Configurado para buscar até 5 chaves no secrets
    chave = st.secrets.get(f"gemini_api_key_{i}") or st.secrets.get("gemini_api_key" if i == 1 else None)
    if chave and chave not in CHAVES_GEMINI:
        CHAVES_GEMINI.append(chave)

# Fallback caso tenha cadastrado apenas a chave padrão "gemini_api_key"
if not CHAVES_GEMINI and "gemini_api_key" in st.secrets:
    CHAVES_GEMINI.append(st.secrets["gemini_api_key"])

if not CHAVES_GEMINI:
    st.error("Nenhuma chave da API do Gemini foi configurada nos segredos do Streamlit.")

# Cria um iterador circular para alternar as chaves infinitamente
roleta_chaves = itertools.cycle(CHAVES_GEMINI) if CHAVES_GEMINI else None

def obter_modelo_rotacionado():
    """Retorna uma instância do modelo configurada com a próxima chave da roleta."""
    if not roleta_chaves:
        return None
    chave_atual = next(roleta_chaves)
    genai.configure(api_key=chave_atual)
    return genai.GenerativeModel('gemini-3.5-flash-lite')

# === 5 NAVEGADORES SIMULTÂNEOS ===
MAX_WORKERS = 5 
sessao_queue = queue.Queue() 

# =========================================================================
# BANCO DE DADOS SISCOMEX (CACHE OTIMIZADO)
# =========================================================================
@st.cache_resource
def iniciar_cliente_ncm():
    """Inicializa o cliente da biblioteca siscomex-ncm uma única vez por sessão."""
    try:
        return FetchNcm()
    except Exception as e:
        st.error(f"Erro ao inicializar o banco do Siscomex: {e}")
        return None

# =========================================================================
# INTELIGÊNCIA ARTIFICIAL - AUDITORIA E CORREÇÃO COM ROTAÇÃO DE CHAVES
# =========================================================================
def ai_analisar_lote(dados_raspados):
    """Analisa os cenários e audita rigorosamente a compatibilidade da NCM."""
    prompt = f"""
    Você é um auditor fiscal federal implacável e especialista em classificação fiscal (NCM). 
    Analise o seguinte lote JSON de produtos e seus cenários de tributação extraídos do portal.
    
    Para cada produto no lote, faça rigorosamente o seguinte:
    
    1. TESTE DE SANIDADE DA NCM (OBRIGATÓRIO): 
       - Analise a "ncm" informada e a "descricao" do produto. 
       - A NCM faz sentido lógico e mercadológico para o produto? (Exemplo clássico de Erro: NCM de Bebida/Refrigerante [Capítulo 22] para um produto que é "Ovos" [Capítulo 04], ou NCM de Eletrônico para um alimento).
       - Se a NCM estiver manifestamente errada ou sem nenhuma relação técnica com o produto, você DEVE retornar obrigatoriamente:
         - "icms": "NCM Incompatível"
         - "cest": "N/A"
         - "pis": "NCM Incompatível"
         - E PARE DE PROCESSAR ESTE ITEM.
    
    2. AVALIAÇÃO DE CENÁRIO (Apenas se a NCM for coerente):
       - Escolha o enquadramento do portal que melhor se adapta ao produto.
    
    3. AUDITORIA DE ICMS-ST E CEST: 
       - Leia o 'texto_icms_bruto' do cenário escolhido.
       - Se aplicar ICMS-ST, responda: "ICMS ST". Caso contrário: "Não".
       - Identifique o código CEST de 7 dígitos correspondente ou responda "N/A".
       
    4. AUDITORIA DE PIS/COFINS: 
       - Leia o 'texto_pis_bruto'. Se mencionar "monofásica" ou "monofásico", responda: "Monofásico". Caso contrário: "Não Monofásico".
    
    Devolva ESTRITAMENTE um array JSON nativo neste exato formato:
    [
      {{
        "id_linha": <mesmo id_linha recebido>,
        "icms": "<ICMS ST, Não ou NCM Incompatível>",
        "cest": "<cest exato auditado ou N/A>",
        "pis": "<Monofásico, Não Monofásico ou NCM Incompatível>"
      }}
    ]
    
    DADOS RASPADOS:
    {json.dumps(dados_raspados, ensure_ascii=False)}
    """
    
    for _ in range(3):
        model_rotativo = obter_modelo_rotacionado()
        if not model_rotativo:
            break
        try:
            res = model_rotativo.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            ).text.strip()
            
            match = re.search(r'\[.*\]', res, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except Exception:
            time.sleep(2)
            
    return []

def sugerir_ncm_correta(descricao_produto, ncm_client):
    """Pede para a IA sugerir a NCM correta utilizando rotação e valida na biblioteca oficial."""
    prompt = f"Qual é a NCM (código de 8 dígitos) mais adequada para o produto: '{descricao_produto}'? Responda APENAS com os 8 números, sem nenhum texto extra ou formatação."
    
    ncm_sugerida = ""
    for _ in range(max(len(CHAVES_GEMINI), 2)):
        model_rotativo = obter_modelo_rotacionado()
        if not model_rotativo:
            break
        try:
            res = model_rotativo.generate_content(prompt).text.strip()
            ncm_sugerida = re.sub(r'\D', '', res)
            
            if len(ncm_sugerida) == 8 and ncm_client:
                ncm_obj = ncm_client.get_codigo_ncm(ncm_sugerida)
                if ncm_obj and ncm_obj.descricao_ncm:
                    desc_limpa = ncm_obj.descricao_ncm.lstrip('- ').strip()
                    return f"{ncm_sugerida} ({desc_limpa})"
                else:
                    return f"{ncm_sugerida} (Não localizada no Siscomex)"
            elif len(ncm_sugerida) == 8:
                return f"{ncm_sugerida} (Formato inválido)"
        except Exception as e:
            erro_str = str(e)
            if "429" in erro_str or "ResourceExhausted" in erro_str:
                continue
            break
            
    return f"{ncm_sugerida or 'Erro'} (Erro ao validar no Siscomex)"

# =========================================================================
# O MOTOR CORE - RASPAGEM CEGA
# =========================================================================
def raspar_cenarios_ncm(ncm_bruta, descricao_produto, uf_codigo, index, session):
    ncm_bruta = str(ncm_bruta).strip()
    ncm_numeros = ncm_bruta.replace(".", "")
    if len(ncm_numeros) == 8:
        ncm_formatada = f"{ncm_numeros[:4]}.{ncm_numeros[4:6]}.{ncm_numeros[6:]}"
    else:
        ncm_formatada = ncm_bruta
        
    url_base = "https://itcnet.com.br/orientador_fiscal/index.php"
    
    try:
        payload_1 = {"uf": uf_codigo, "pesquisa": ncm_formatada, "passo": "1", "local": "1"}
        res_passo1 = session.post(url_base, data=payload_1, timeout=15)
        soup_1 = BeautifulSoup(res_passo1.text, "html.parser")
        
        form_alvo = soup_1.find("form", attrs={"name": "selecionar"})
        if not form_alvo:
            return index, {"erro": "NCM não encontrada no portal ITC"}
            
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
                return index, {"erro": "NCM não encontrada no portal ITC"}
        
        cenarios_extraidos = []
        for cod_tributacao, desc_opcao in opcoes_tributacao.items():
            payload_2 = {
                "uf": uf_codigo, "estado": "", "pesquisa": ncm_formatada,
                "tributacao_cod": cod_tributacao, "passo": "2", "local": "1",
                "posicao_tipi": "1", "descricao": ""
            }
            session.post(url_base, data=payload_2, timeout=15) 
            
            url_icms_st = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=2&passo=2"
            texto_icms_st = BeautifulSoup(session.get(url_icms_st, timeout=15).text, "html.parser").find("div", class_="panel-primary")
            texto_icms_st = texto_icms_st.get_text(separator=' ', strip=True) if texto_icms_st else ""
            
            url_pis = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=3&passo=2"
            texto_pis = BeautifulSoup(session.get(url_pis, timeout=15).text, "html.parser").find("div", class_="panel-primary")
            texto_pis = texto_pis.get_text(separator=' ', strip=True) if texto_pis else ""
            
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
st.set_page_config(page_title="Auditor Fiscal IA", page_icon="⚡", layout="wide")

if "processado" not in st.session_state:
    st.session_state.processado = False
    st.session_state.df_resultado = None
    st.session_state.planilha_bytes = None

st.title("⚡ Auditor Fiscal IA - Tributação & NCM em Lote (Multi-API)")

# Instancia o cliente da biblioteca do Siscomex (aproveitando o cache)
cliente_ncm = iniciar_cliente_ncm()

if not st.session_state.processado:
    estados_dict = {
        "1": "Santa Catarina - Nova versão", "28": "Santa Catarina",
        "2": "Rio Grande do Sul", "3": "Paraná", "4": "São Paulo",
        "5": "Minas Gerais", "6": "Rio de Janeiro", "9": "Espírito Santo - Nova versão"
    }
    
    uf_selecionada = st.selectbox(
        "Selecione o Estado (UF) para consulta:", 
        options=list(estados_dict.keys()), 
        format_func=lambda x: estados_dict[x],
        index=1 
    )
    
    st.markdown("---")
    st.subheader("1. Baixe a Planilha Modelo")
    st.markdown("A planilha deve conter as colunas **NCM** e **Descricao**.")
    
    df_modelo = pd.DataFrame({'NCM': ['22011000', '84713012'], 'Descricao': ['Agua Mineral 500ml', 'Pneu de Moto aro 18 (Erro Proposital)']})
    buffer_modelo = io.BytesIO()
    with pd.ExcelWriter(buffer_modelo, engine='openpyxl') as writer:
        df_modelo.to_excel(writer, index=False)
        
    st.download_button(label="⬇️ Baixar Planilha Modelo", data=buffer_modelo.getvalue(), file_name="modelo_ncm.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="secondary", width="stretch")
    
    st.markdown("---")
    st.subheader("2. Suba a Base de Produtos")
    arquivo_up = st.file_uploader("Upload da Planilha (.xlsx)", type=["xlsx"])

    if st.button("Iniciar Auditoria 🚀", type="primary", width="stretch"):
        if arquivo_up is None:
            st.warning("Faça o upload da planilha.")
        else:
            df = pd.read_excel(arquivo_up)
            
            if "NCM" not in df.columns or "Descricao" not in df.columns:
                st.error("A planilha precisa ter obrigatoriamente as colunas 'NCM' e 'Descricao'.")
                st.stop()
            
            df = df.dropna(subset=['NCM'])
            
            # Prepara as colunas
            if "ICMS_ST" not in df.columns: df["ICMS_ST"] = ""
            if "PIS_COFINS" not in df.columns: df["PIS_COFINS"] = ""
            if "CEST" not in df.columns: df["CEST"] = ""
            if "NCM_Sugerida_Siscomex" not in df.columns: df["NCM_Sugerida_Siscomex"] = ""
                
            total_linhas = len(df)
            status_text = st.empty()
            progress_bar = st.progress(0)
            
            try:
                status_text.info(f"🔐 Inicializando robôs... Criando {MAX_WORKERS} conexões.")
                
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
                        sessao_http.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://itcnet.com.br/"})
                        sessao_queue.put(sessao_http)
                    
                    browser.close()
                status_text.success("🔥 Conexões criadas! Iniciando varredura profunda no portal...")
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
                    status_text.text(f"Auditando bases: {concluidos} de {total_linhas} NCMs extraídas...")

            # FASE 2: AVALIAÇÃO DA IA E CORREÇÃO SISCOMEX EM LOTES COM ROTAÇÃO DE CHAVES
            if lote_para_ia:
                resultados_ia = []
                TAMANHO_LOTE = 200
                
                for i in range(0, len(lote_para_ia), TAMANHO_LOTE):
                    pedaco = lote_para_ia[i : i + TAMANHO_LOTE]
                    fim_lote = min(i + TAMANHO_LOTE, len(lote_para_ia))
                    
                    status_text.info(f"🧠 IA cruzando regras tributárias (Multi-API): Avaliando pacote {i + 1} até {fim_lote} de {len(lote_para_ia)}...")
                    
                    resposta_pedaco = ai_analisar_lote(pedaco)
                    resultados_ia.extend(resposta_pedaco)
                
                status_text.info("🛠️ Análise concluída. Aplicando resultados e buscando correções oficiais no Siscomex...")
                
                for item in resultados_ia:
                    idx = item.get("id_linha")
                    if idx is not None and idx in df.index:
                        status_icms = item.get("icms", "Erro IA")
                        df.at[idx, "ICMS_ST"] = status_icms
                        df.at[idx, "CEST"] = item.get("cest", "Erro IA")
                        df.at[idx, "PIS_COFINS"] = item.get("pis", "Erro IA")
                        
                        # Gatilho de Autocorreção: NCM Errada!
                        if "Incompatível" in status_icms:
                            status_text.warning(f"⚠️ Alerta: NCM errada detectada na linha {idx}. Buscando correção oficial...")
                            desc_cliente = df.at[idx, "Descricao"]
                            sugestao = sugerir_ncm_correta(desc_cliente, cliente_ncm)
                            df.at[idx, "NCM_Sugerida_Siscomex"] = sugestao

            for idx, msg_erro in erros_raspagem.items():
                df.at[idx, "ICMS_ST"] = msg_erro
                df.at[idx, "CEST"] = "N/A"
                df.at[idx, "PIS_COFINS"] = msg_erro
            
            status_text.success("✅ Relatório auditado com sucesso!")
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            
            st.session_state.df_resultado = df
            st.session_state.planilha_bytes = output.getvalue()
            st.session_state.processado = True
            
            st.rerun()

else:
    st.success("✅ Auditoria inteligente concluída!")
    
    st.dataframe(st.session_state.df_resultado, width="stretch")
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="📥 Baixar Auditoria Final",
            data=st.session_state.planilha_bytes,
            file_name="auditoria_fiscal_inteligente.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch"
        )
    with col2:
        if st.button("🔄 Nova Auditoria", width="stretch"):
            st.session_state.processado = False
            st.session_state.df_resultado = None
            st.session_state.planilha_bytes = None
            st.rerun()
