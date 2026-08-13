import pandas as pd
import requests
import concurrent.futures
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import sys
import streamlit as st
import io
import os
import queue

# TRUQUE PARA A NUVEM: Força a instalação do navegador invisível no servidor do Streamlit
os.system("playwright install chromium")

USUARIO_ITC = st.secrets["USUARIO_ITC"]
SENHA_ITC = st.secrets["SENHA_ITC"]

# === A MÁGICA DA SUA IDEIA: 5 NAVEGADORES SIMULTÂNEOS ===
MAX_WORKERS = 5 
sessao_queue = queue.Queue() # Fila que vai guardar os "crachás"

# =========================================================================
# O SEU MOTOR INTACTO (LÓGICA DE EXTRAÇÃO PRESERVADA)
# =========================================================================
def processar_ncm_core(ncm_bruta, index, session):
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
            "uf": "28",
            "pesquisa": ncm_formatada, 
            "passo": "1",
            "local": "1"
        }
        res_passo1 = session.post(url_base, data=payload_1, timeout=15)
        soup_1 = BeautifulSoup(res_passo1.text, "html.parser")
        
        form_alvo = soup_1.find("form", attrs={"name": "selecionar"})
        if not form_alvo:
            return index, "NCM não encontrada", "NCM não encontrada"
            
        tributacao_cod = form_alvo.find("input", attrs={"name": "tributacao_cod"})["value"]
        
        # === PASSO 2: O Payload Secreto (Simula o clique em Prosseguir) ===
        payload_2 = {
            "uf": "28",
            "estado": "",
            "pesquisa": ncm_formatada,
            "tributacao_cod": tributacao_cod,
            "passo": "2",
            "local": "1",
            "posicao_tipi": "1",
            "descricao": ""
        }
        session.post(url_base, data=payload_2, timeout=15) 
        
        # === PASSO 3: Puxa o texto da Aba ICMS/ST (aba=2) ===
        url_icms_st = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=2&passo=2"
        res_icms = session.get(url_icms_st, timeout=15)
        soup_icms = BeautifulSoup(res_icms.text, "html.parser")
        
        painel_icms = soup_icms.find("div", class_="panel-primary")
        texto_icms_st = painel_icms.get_text(separator=' ', strip=True) if painel_icms else ""
        
        # === PASSO 4: Puxa o texto da Aba PIS/COFINS (aba=3) ===
        url_pis = f"https://itcnet.com.br/orientador_fiscal/index.php?ncm={ncm_formatada}&aba=3&passo=2"
        res_pis = session.get(url_pis, timeout=15)
        soup_pis = BeautifulSoup(res_pis.text, "html.parser")
        
        painel_pis = soup_pis.find("div", class_="panel-primary")
        texto_pis = painel_pis.get_text(separator=' ', strip=True) if painel_pis else ""
        
        # === LÓGICA DE FILTRAGEM BLINDADA ===
        texto_pis_min = texto_pis.lower()
        texto_icms_min = texto_icms_st.lower()
        texto_icms_limpo = " ".join(texto_icms_min.split())

        termos_monofasico = ["monofásica", "monofasica", "monofásico", "monofasico"]
        tem_monofasico = any(termo in texto_pis_min for termo in termos_monofasico)
        
        frase_isencao = "não está sujeita ao regime de substituição tributária"
        tem_regra_icms = (frase_isencao not in texto_icms_limpo) and (len(texto_icms_limpo) > 15)

        if not tem_monofasico and not tem_regra_icms:
            return index, "Fora da Regra", "Fora da Regra"
            
        icms_salvar = texto_icms_st if tem_regra_icms else "Fora da Regra"
        pis_salvar = texto_pis if tem_monofasico else "Fora da Regra"
        
        return index, icms_salvar, pis_salvar

    except Exception as e:
        return index, f"Erro ao processar", f"Erro ao processar"


def processar_ncm_fila(ncm_bruta, index):
    """Pega uma sessão livre da fila, pesquisa, e devolve a sessão para a fila."""
    sessao_ativa = sessao_queue.get() 
    try:
        return processar_ncm_core(ncm_bruta, index, sessao_ativa)
    finally:
        sessao_queue.put(sessao_ativa) 


# =========================================================================
# INTERFACE STREAMLIT COM MEMÓRIA DE ESTADO 
# =========================================================================
st.set_page_config(page_title="Validador NCM", page_icon="⚡", layout="centered")

if "processado" not in st.session_state:
    st.session_state.processado = False
    st.session_state.df_resultado = None
    st.session_state.planilha_bytes = None

st.title("⚡ Robô Fiscal - Tributação NCM")

if not st.session_state.processado:
    st.markdown("Cole ou digite os códigos NCM abaixo para fazer a varredura em massa.")

    texto_ncms = st.text_area(
        "Digite as NCMs (uma abaixo da outra):", 
        height=200, 
        placeholder="Exemplo:\n85365090\n39222000"
    )

    if st.button("Iniciar Varredura 🚀", type="primary", use_container_width=True):
        
        if not texto_ncms.strip():
            st.warning("Por favor, digite pelo menos uma NCM antes de iniciar.")
        else:
            lista_ncms_original = [ncm.strip() for ncm in texto_ncms.split('\n') if ncm.strip()]
            lista_ncms_unicas = []
            ncms_vistas = set()
            
            for ncm in lista_ncms_original:
                ncm_numeros = ncm.replace(".", "")
                if ncm_numeros not in ncms_vistas:
                    ncms_vistas.add(ncm_numeros)
                    lista_ncms_unicas.append(ncm) 
            
            duplicadas = len(lista_ncms_original) - len(lista_ncms_unicas)
            if duplicadas > 0:
                st.toast(f"🧹 {duplicadas} NCM(s) duplicada(s) removida(s) automaticamente!", icon="✅")

            df = pd.DataFrame({"NCM": lista_ncms_unicas})
            
            if "ICMS_ST" not in df.columns:
                df["ICMS_ST"] = ""
            if "PIS_COFINS" not in df.columns:
                df["PIS_COFINS"] = ""
                
            total_linhas = df['NCM'].notna().sum()
            
            status_text = st.empty()
            progress_bar = st.progress(0)
            
            try:
                status_text.info(f"🔐 Aquecendo os motores... Criando {MAX_WORKERS} acessos simultâneos (aguarde ~20 segundos).")
                
                # --- A GERAÇÃO DO POOL DE CONEXÕES ---
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
                        
                        # Prepara a sessão de requisição rápida e guarda na fila
                        sessao_http = requests.Session()
                        sessao_http.cookies.update(cookie_dict)
                        sessao_http.headers.update({
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Referer": "https://itcnet.com.br/acesso.php?modulo=orientador_fiscal"
                        })
                        sessao_http.get("https://itcnet.com.br/acesso.php?modulo=orientador_fiscal", timeout=15)
                        sessao_queue.put(sessao_http)
                    
                    browser.close()
                # -------------------------------------
                
                status_text.success("🔥 Todos os acessos aprovados! Iniciando varredura em velocidade máxima...")
            except Exception as e:
                status_text.error(f"Erro na criação dos acessos. Verifique o portal. Detalhe: {e}")
                st.stop()

            resultados = {}
            concluidos = 0
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futuros = []
                for index, row in df.iterrows():
                    if pd.isna(row["NCM"]) or str(row["NCM"]).strip() == "":
                        continue
                    # Agora usamos a função nova que administra a fila
                    futuro = executor.submit(processar_ncm_fila, row["NCM"], index)
                    futuros.append(futuro)
                    
                for futuro in concurrent.futures.as_completed(futuros):
                    idx, val_icms, val_pis = futuro.result()
                    resultados[idx] = {"icms": val_icms, "pis": val_pis}
                    
                    concluidos += 1
                    progress = int((concluidos / total_linhas) * 100)
                    progress_bar.progress(progress)
                    status_text.text(f"Processando: {concluidos} de {total_linhas} NCMs concluídas...")

            for idx, dados in resultados.items():
                df.at[idx, "ICMS_ST"] = dados["icms"]
                df.at[idx, "PIS_COFINS"] = dados["pis"]
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            
            st.session_state.df_resultado = df
            st.session_state.planilha_bytes = output.getvalue()
            st.session_state.processado = True
            
            st.rerun()

else:
    st.success("✅ Varredura concluída com sucesso! Os resultados ficarão congelados aqui.")
    
    st.dataframe(st.session_state.df_resultado, use_container_width=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.download_button(
            label="📥 Baixar Resultado",
            data=st.session_state.planilha_bytes,
            file_name="lista_ncm_atualizada.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True
        )
        
    with col2:
        if st.button("🔄 Nova Consulta", use_container_width=True):
            st.session_state.processado = False
            st.session_state.df_resultado = None
            st.session_state.planilha_bytes = None
            st.rerun()
