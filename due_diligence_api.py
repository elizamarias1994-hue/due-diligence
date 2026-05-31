"""
ASSISTENTE DE DUE DILIGENCE CORPORATIVA — BACKEND
Integra APIs gratuitas: BrasilAPI, Portal Transparência (CGU),
sanctions.network (OFAC+ONU+EU), OpenSanctions, OpenCorporates.

INSTALAÇÃO:
    pip install fastapi uvicorn httpx python-dotenv

EXECUÇÃO:
    uvicorn due_diligence_api:app --host 0.0.0.0 --port 8000

VARIÁVEIS DE AMBIENTE (arquivo .env):
    CGU_API_KEY=sua_chave_portal_transparencia   # obrigatória para CEIS/CNEP
    OPENSANCTIONS_API_KEY=sua_chave_trial        # opcional, €0,10/consulta

OBTER CHAVE CGU:
    Acesse: https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email
    Autentique via gov.br (conta Prata/Ouro) e receba o token por e-mail.

OBTER CHAVE OPENSANCTIONS (30 dias grátis):
    Acesse: https://www.opensanctions.org/account
    Cadastre com e-mail corporativo e receba trial key.
"""

import os
import re
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# ─── Configuração ────────────────────────────────────────────────────────────

CGU_API_KEY         = os.getenv("CGU_API_KEY", "")
OPENSANCTIONS_KEY   = os.getenv("OPENSANCTIONS_API_KEY", "")

BASE_BRASILAPI      = "https://brasilapi.com.br/api"
BASE_CGU            = "https://api.portaldatransparencia.gov.br/api-de-dados"
BASE_SANCTIONS_NET  = "https://sanctions.network"
BASE_OPENSANCTIONS  = "https://api.opensanctions.org"
BASE_OPENCORP       = "https://api.opencorporates.com/v0.4"

TIMEOUT = 15

app = FastAPI(
    title="Due Diligence API",
    description="Assistente de due diligence corporativa — APIs gratuitas brasileiras e internacionais.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ─── Utilitários ─────────────────────────────────────────────────────────────

def limpar_cnpj(cnpj: str) -> str:
    """Remove pontuação e retorna 14 dígitos."""
    return re.sub(r"\D", "", cnpj)

def limpar_cpf(cpf: str) -> str:
    """Remove pontuação e retorna 11 dígitos."""
    return re.sub(r"\D", "", cpf)

def cgu_headers() -> dict:
    if not CGU_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="CGU_API_KEY não configurada. Cadastre-se em: "
                   "https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
        )
    return {"chave-api-dados": CGU_API_KEY, "Accept": "application/json"}

async def get(url: str, params: dict = None, headers: dict = None) -> dict | list:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

# ─── 1. CNPJ — BrasilAPI ─────────────────────────────────────────────────────

@app.get("/cnpj/{cnpj}", summary="Dados cadastrais e quadro societário (QSA) de CNPJ")
async def consultar_cnpj(cnpj: str):
    """
    Retorna razão social, situação cadastral, endereço, capital social,
    CNAE, quadro societário (sócios e administradores), porte e regime tributário.

    Fonte: BrasilAPI → Receita Federal do Brasil.
    Custo: gratuito, sem autenticação.
    Limite: ~3 requisições/minuto por IP no plano gratuito.
    """
    cnpj_limpo = limpar_cnpj(cnpj)
    if len(cnpj_limpo) != 14:
        raise HTTPException(status_code=400, detail="CNPJ inválido. Informe 14 dígitos.")

    data = await get(
        f"{BASE_BRASILAPI}/cnpj/v1/{cnpj_limpo}",
        headers={"User-Agent": "DueDiligenceAssistant/1.0"},
    )

    if not data:
        raise HTTPException(status_code=404, detail=f"CNPJ {cnpj_limpo} não encontrado.")

    # Normaliza resposta
    return {
        "fonte": "BrasilAPI / Receita Federal",
        "cnpj": data.get("cnpj"),
        "razao_social": data.get("razao_social"),
        "nome_fantasia": data.get("nome_fantasia"),
        "situacao_cadastral": data.get("descricao_situacao_cadastral"),
        "data_situacao": data.get("data_situacao_cadastral"),
        "data_abertura": data.get("data_inicio_atividade"),
        "natureza_juridica": data.get("natureza_juridica"),
        "porte": data.get("porte"),
        "capital_social": data.get("capital_social"),
        "cnae_principal": data.get("cnae_fiscal_descricao"),
        "logradouro": f"{data.get('logradouro', '')}, {data.get('numero', '')} "
                      f"{data.get('complemento', '')} — {data.get('municipio', '')}/{data.get('uf', '')}",
        "cep": data.get("cep"),
        "telefone": data.get("ddd_telefone_1"),
        "email": data.get("email"),
        "quadro_societario": [
            {
                "nome": s.get("nome_socio"),
                "qualificacao": s.get("qualificacao_socio"),
                "cpf_cnpj": s.get("cnpj_cpf_do_socio"),
                "faixa_etaria": s.get("faixa_etaria"),
                "data_entrada": s.get("data_entrada_sociedade"),
            }
            for s in data.get("qsa", [])
        ],
        "simples_nacional": data.get("opcao_pelo_simples"),
        "mei": data.get("opcao_pelo_mei"),
    }

# ─── 2. CEIS — Portal da Transparência (CGU) ─────────────────────────────────

@app.get("/sancoes/ceis", summary="CEIS — Empresas e pessoas inidôneas/suspensas")
async def consultar_ceis(
    cnpj_cpf: str = Query(..., description="CNPJ (14 dígitos) ou CPF (11 dígitos) sem pontuação"),
    pagina: int = 1,
):
    """
    Consulta o Cadastro Nacional de Empresas Inidôneas e Suspensas (CEIS).
    Retorna sanções que implicam restrição de licitar ou contratar com a
    Administração Pública (Lei 8.666/93 e Lei 14.133/2021).

    Fonte: CGU — Portal da Transparência.
    Requer: CGU_API_KEY (obter em portaldatransparencia.gov.br).
    Custo: gratuito após cadastro gov.br.
    """
    doc = re.sub(r"\D", "", cnpj_cpf)
    data = await get(
        f"{BASE_CGU}/ceis",
        params={"cpfCnpj": doc, "pagina": pagina},
        headers=cgu_headers(),
    )
    return {
        "fonte": "CEIS — Portal da Transparência / CGU",
        "documento": doc,
        "pagina": pagina,
        "total_registros": len(data) if isinstance(data, list) else 0,
        "resultado": data if isinstance(data, list) else [],
    }

# ─── 3. CNEP — Portal da Transparência (CGU) ─────────────────────────────────

@app.get("/sancoes/cnep", summary="CNEP — Empresas punidas pela Lei Anticorrupção")
async def consultar_cnep(
    cnpj_cpf: str = Query(..., description="CNPJ ou CPF sem pontuação"),
    pagina: int = Query(1, ge=1),
):
    """
    Consulta o Cadastro Nacional de Empresas Punidas (CNEP).
    Registra sanções da Lei 12.846/2013 (Lei Anticorrupção).

    Fonte: CGU — Portal da Transparência.
    Requer: CGU_API_KEY.
    """
    doc = re.sub(r"\D", "", cnpj_cpf)
    data = await get(
        f"{BASE_CGU}/cnep",
        params={"cpfCnpj": doc, "pagina": pagina},
        headers=cgu_headers(),
    )
    return {
        "fonte": "CNEP — Portal da Transparência / CGU",
        "documento": doc,
        "resultado": data if isinstance(data, list) else [],
    }

# ─── 4. CEPIM — Portal da Transparência (CGU) ────────────────────────────────

@app.get("/sancoes/cepim", summary="CEPIM — Entidades sem fins lucrativos impedidas")
async def consultar_cepim(
    cnpj: str = Query(..., description="CNPJ sem pontuação"),
    pagina: int = Query(1, ge=1),
):
    """
    Consulta o Cadastro de Entidades Privadas sem Fins Lucrativos Impedidas (CEPIM).
    Essencial para due diligence de OSs, OSCIPs, fundações e associações.

    Fonte: CGU — Portal da Transparência.
    Requer: CGU_API_KEY.
    """
    cnpj_limpo = limpar_cnpj(cnpj)
    data = await get(
        f"{BASE_CGU}/cepim",
        params={"cnpj": cnpj_limpo, "pagina": pagina},
        headers=cgu_headers(),
    )
    return {
        "fonte": "CEPIM — Portal da Transparência / CGU",
        "cnpj": cnpj_limpo,
        "resultado": data if isinstance(data, list) else [],
    }

# ─── 5. Contratos Públicos — CGU ──────────────────────────────────────────────

@app.get("/contratos", summary="Contratos com o Poder Executivo Federal")
async def consultar_contratos(
    cnpj_cpf: str = Query(..., description="CNPJ ou CPF do fornecedor"),
    pagina: int = Query(1, ge=1),
):
    """
    Retorna contratos celebrados com o Poder Executivo Federal pelo CNPJ/CPF informado.
    Útil para mapear histórico de contratações públicas e eventual superfaturamento.

    Fonte: CGU — Portal da Transparência.
    Requer: CGU_API_KEY.
    """
    doc = re.sub(r"\D", "", cnpj_cpf)
    data = await get(
        f"{BASE_CGU}/contratos",
        params={"cnpjCpfFornecedor": doc, "pagina": pagina},
        headers=cgu_headers(),
    )
    return {
        "fonte": "Contratos — Portal da Transparência / CGU",
        "documento": doc,
        "resultado": data if isinstance(data, list) else [],
    }

# ─── 6. Sanções Internacionais — sanctions.network ───────────────────────────

@app.get("/sancoes/internacionais", summary="OFAC + ONU + EU — sanções internacionais")
async def consultar_sancoes_internacionais(
    nome: str = Query(..., description="Nome da pessoa física ou jurídica"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    Consulta gratuita contra as três principais listas de sanções internacionais:
    - OFAC SDN (EUA — Office of Foreign Assets Control)
    - UN Security Council Consolidated List (ONU)
    - EU Financial Sanctions File (União Europeia)

    Fonte: sanctions.network — API pública, sem autenticação, 100% gratuita.
    Método: busca por nome com correspondência fuzzy.
    """
    data = await get(
        f"{BASE_SANCTIONS_NET}/sanctions",
        params={"name": nome, "limit": limit},
    )
    registros = data if isinstance(data, list) else data.get("results", [])
    return {
        "fonte": "sanctions.network (OFAC + ONU + EU)",
        "termo_buscado": nome,
        "total_encontrado": len(registros),
        "resultado": registros,
    }

# ─── 7. OpenSanctions — PEP + Sanções (trial/pago) ───────────────────────────

@app.get("/sancoes/opensanctions", summary="OpenSanctions — PEP + sanções 200+ fontes")
async def consultar_opensanctions(
    nome: str = Query(..., description="Nome da pessoa ou empresa"),
    schema: str = Query("Thing", description="Tipo: Person, Company, Thing"),
):
    """
    Consulta o OpenSanctions: maior base open de PEPs e sanções do mundo.
    Cobre 200+ fontes globais incluindo OFAC, ONU, EU, SECO, CCFPI e listas nacionais.

    Requer: OPENSANCTIONS_API_KEY (trial 30 dias gratuito com e-mail corporativo).
    Custo: €0,10 por consulta na API gerenciada.
    Self-hosted: gratuito para uso interno não-comercial.
    Obter trial: https://www.opensanctions.org/account
    """
    if not OPENSANCTIONS_KEY:
        raise HTTPException(
            status_code=503,
            detail="OPENSANCTIONS_API_KEY não configurada. "
                   "Obtenha trial em: https://www.opensanctions.org/account",
        )
    headers = {"Authorization": f"ApiKey {OPENSANCTIONS_KEY}", "Accept": "application/json"}
    data = await get(
        f"{BASE_OPENSANCTIONS}/match/default",
        params={},
        headers=headers,
    )
    # OpenSanctions usa POST para matching — chamada simplificada via search
    search_data = await get(
        f"{BASE_OPENSANCTIONS}/search/default",
        params={"q": nome, "schema": schema, "limit": 10},
        headers=headers,
    )
    resultados = search_data.get("results", []) if isinstance(search_data, dict) else []
    return {
        "fonte": "OpenSanctions — 200+ listas mundiais",
        "termo_buscado": nome,
        "total": search_data.get("total", {}).get("value", len(resultados)) if isinstance(search_data, dict) else 0,
        "resultado": [
            {
                "id": e.get("id"),
                "nome": e.get("caption"),
                "schema": e.get("schema"),
                "datasets": e.get("datasets", []),
                "paises": e.get("properties", {}).get("country", []),
                "score": e.get("score"),
            }
            for e in resultados
        ],
    }

# ─── 8. OpenCorporates — Estrutura Societária Internacional ──────────────────

@app.get("/empresa/internacional", summary="Estrutura societária global via OpenCorporates")
async def consultar_opencorporates(
    nome: str = Query(None, description="Nome da empresa"),
    numero: str = Query(None, description="Número de registro"),
    jurisdicao: str = Query(None, description="Código da jurisdição, ex: br, us, gb"),
):
    """
    Consulta o OpenCorporates: 230 milhões de entidades em 145 jurisdições.
    Útil para identificar empresas relacionadas, grupo econômico e sócias estrangeiras.

    Fonte: OpenCorporates.
    Custo: gratuito até 200 requisições/mês (50/dia) com API key free.
    API key free: https://opencorporates.com/users/sign_up
    Plano pago: £2.250/ano para uso comercial intenso.
    """
    params: dict = {"format": "json"}
    if nome:
        params["q"] = nome
    if jurisdicao:
        params["jurisdiction_code"] = jurisdicao
    if numero:
        params["company_number"] = numero

    data = await get(f"{BASE_OPENCORP}/companies/search", params=params)
    companies = (
        data.get("results", {}).get("companies", [])
        if isinstance(data, dict) else []
    )
    return {
        "fonte": "OpenCorporates — 230M+ entidades, 145 jurisdições",
        "total": data.get("results", {}).get("total_count", 0) if isinstance(data, dict) else 0,
        "resultado": [
            {
                "nome": c.get("company", {}).get("name"),
                "numero_registro": c.get("company", {}).get("company_number"),
                "jurisdicao": c.get("company", {}).get("jurisdiction_code"),
                "status": c.get("company", {}).get("current_status"),
                "data_constituicao": c.get("company", {}).get("incorporation_date"),
                "tipo": c.get("company", {}).get("company_type"),
                "url": c.get("company", {}).get("opencorporates_url"),
            }
            for c in companies
        ],
    }

# ─── 9. Consulta consolidada ─────────────────────────────────────────────────

@app.get("/consulta-completa/cnpj/{cnpj}", summary="Relatório consolidado por CNPJ")
async def consulta_completa_cnpj(cnpj: str):
    """
    Executa consulta consolidada em todas as bases disponíveis para um CNPJ:
    1. Dados cadastrais + QSA (BrasilAPI)
    2. CEIS — sanções administrativas (CGU)
    3. CNEP — Lei Anticorrupção (CGU)
    4. CEPIM — impedimentos sem fins lucrativos (CGU)
    5. Contratos públicos federais (CGU)
    6. Sanções internacionais pela razão social (sanctions.network)

    Retorna objeto unificado pronto para análise pelo GPT.
    """
    cnpj_limpo = limpar_cnpj(cnpj)
    resultado: dict = {"cnpj": cnpj_limpo, "erros": []}

    # 1. Dados cadastrais
    try:
        resultado["dados_cadastrais"] = await consultar_cnpj(cnpj_limpo)
        razao_social = resultado["dados_cadastrais"].get("razao_social", "")
    except Exception as e:
        resultado["erros"].append(f"BrasilAPI: {str(e)}")
        razao_social = ""

    # 2. CEIS
    if CGU_API_KEY:
        try:
            resultado["ceis"] = await consultar_ceis(cnpj_limpo)
        except Exception as e:
            resultado["erros"].append(f"CEIS: {str(e)}")

    # 3. CNEP
    if CGU_API_KEY:
        try:
            resultado["cnep"] = await consultar_cnep(cnpj_limpo)
        except Exception as e:
            resultado["erros"].append(f"CNEP: {str(e)}")

    # 4. CEPIM
    if CGU_API_KEY:
        try:
            resultado["cepim"] = await consultar_cepim(cnpj_limpo)
        except Exception as e:
            resultado["erros"].append(f"CEPIM: {str(e)}")

    # 5. Contratos públicos
    if CGU_API_KEY:
        try:
            resultado["contratos_publicos"] = await consultar_contratos(cnpj_limpo)
        except Exception as e:
            resultado["erros"].append(f"Contratos: {str(e)}")

    # 6. Sanções internacionais pela razão social
    if razao_social:
        try:
            resultado["sancoes_internacionais"] = await consultar_sancoes_internacionais(razao_social)
        except Exception as e:
            resultado["erros"].append(f"sanctions.network: {str(e)}")

    # Score de risco simplificado
    flags = 0
    if resultado.get("ceis", {}).get("total_registros", 0) > 0:
        flags += 3
    if resultado.get("cnep", {}).get("resultado"):
        flags += 3
    if resultado.get("cepim", {}).get("resultado"):
        flags += 2
    if resultado.get("sancoes_internacionais", {}).get("total_encontrado", 0) > 0:
        flags += 4
    sit = resultado.get("dados_cadastrais", {}).get("situacao_cadastral", "")
    if sit and "ATIVA" not in sit.upper():
        flags += 1

    if flags == 0:
        risco = "BAIXO"
    elif flags <= 2:
        risco = "MÉDIO"
    elif flags <= 5:
        risco = "ALTO"
    else:
        risco = "CRÍTICO"

    resultado["score_risco"] = {"nivel": risco, "pontos": flags}
    return resultado


# ─── 10. Consulta consolidada por nome ───────────────────────────────────────

@app.get("/consulta-completa/nome", summary="Relatório consolidado por nome (PF ou PJ)")
async def consulta_completa_nome(
    nome: str = Query(..., description="Nome completo da pessoa física ou razão social"),
    cpf: str = Query(None, description="CPF para consulta de sanções nacionais (opcional)"),
):
    """
    Consulta consolidada por nome:
    1. Sanções internacionais — sanctions.network (OFAC+ONU+EU)
    2. OpenSanctions — PEP + sanções globais (se key configurada)
    3. OpenCorporates — empresas relacionadas internacionais
    4. CEIS/CNEP por CPF — se CPF fornecido e CGU_API_KEY configurada
    """
    resultado: dict = {"nome": nome, "erros": []}

    # Sanções internacionais
    try:
        resultado["sancoes_internacionais"] = await consultar_sancoes_internacionais(nome)
    except Exception as e:
        resultado["erros"].append(f"sanctions.network: {str(e)}")

    # OpenSanctions
    if OPENSANCTIONS_KEY:
        try:
            resultado["opensanctions"] = await consultar_opensanctions(nome)
        except Exception as e:
            resultado["erros"].append(f"OpenSanctions: {str(e)}")

    # OpenCorporates
    try:
        resultado["empresas_relacionadas_intl"] = await consultar_opencorporates(nome=nome)
    except Exception as e:
        resultado["erros"].append(f"OpenCorporates: {str(e)}")

    # Sanções nacionais por CPF
    if cpf and CGU_API_KEY:
        cpf_limpo = limpar_cpf(cpf)
        try:
            resultado["ceis"] = await consultar_ceis(cpf_limpo)
        except Exception as e:
            resultado["erros"].append(f"CEIS: {str(e)}")
        try:
            resultado["cnep"] = await consultar_cnep(cpf_limpo)
        except Exception as e:
            resultado["erros"].append(f"CNEP: {str(e)}")

    return resultado


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health():
    return {
        "status": "ok",
        "cgu_key_configurada": bool(CGU_API_KEY),
        "opensanctions_key_configurada": bool(OPENSANCTIONS_KEY),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("due_diligence_api:app", host="0.0.0.0", port=8000, reload=True)
