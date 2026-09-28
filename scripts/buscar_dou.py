#!/usr/bin/env python3
"""
Buscador - varredura diária de publicações oficiais por palavras-chave.

Fontes (cada uma é um "coletor" independente; se uma falhar, as outras seguem):
  * DOU        - Diário Oficial da União (Leitura do Jornal, in.gov.br)
  * Notas MS   - Notas Técnicas e Informativas do Ministério da Saúde (gov.br/saude)

Roda no GitHub Actions todo dia às 08:00 (horário de Brasília), mas também
funciona no seu computador:

    python scripts/buscar_dou.py                   # hoje (e ontem, se for dia útil)
    python scripts/buscar_dou.py --data 22-09-2026 # uma data específica

Como funciona
-------------
1. Para cada seção configurada (do1, do2, do3), baixa a página pública
   "Leitura do Jornal" da Imprensa Nacional:
       https://www.in.gov.br/leiturajornal?data=DD-MM-AAAA&secao=do1
   Essa página já traz, embutido no HTML (<script id="params">), o JSON com
   TODAS as matérias publicadas naquela seção/dia.
2. Filtra localmente as matérias que citam alguma palavra-chave de
   config.json (sem diferenciar maiúsculas/minúsculas nem acentos).
3. Acumula tudo em dados.json, que o index.html lê para montar o painel.

Se o site do in.gov.br mudar ou bloquear o acesso, o erro fica registrado na
tabela "Execuções" do painel e o workflow do GitHub avisa por e-mail.
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DADOS_PATH = os.path.join(BASE_DIR, "dados.json")

URL_LEITURA = "https://www.in.gov.br/leiturajornal"
URL_MATERIA = "https://www.in.gov.br/web/dou/-/"
URL_NOTAS_MS = "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/notas-tecnicas/{ano}"
ITENS_POR_PAGINA_NOTAS = 30
FUSO_BRASILIA = timezone(timedelta(hours=-3))

MAX_PUBS = 500
MAX_RUNS = 60
MAX_RESUMO = 600
TENTATIVAS = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


class ErroDOU(Exception):
    """Falha ao obter ou interpretar a página do DOU."""


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------

def limpar(texto):
    """Remove tags HTML, entidades e espaços repetidos."""
    if not texto:
        return ""
    texto = html.unescape(str(texto))
    texto = re.sub(r"<[^>]+>", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def normalizar(texto):
    """Minúsculas e sem acentos, para comparar 'Atenção' com 'atencao'."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.casefold()


def compilar_termos(termos):
    """Devolve [(termo_original, regex)] — casa a expressão inteira, não pedaços de palavra."""
    compilados = []
    for termo in termos:
        partes = normalizar(termo).split()
        if not partes:
            continue
        padrao = r"(?<!\w)" + r"\s+".join(re.escape(p) for p in partes) + r"(?!\w)"
        compilados.append((termo, re.compile(padrao)))
    return compilados


def termos_no_texto(texto, compilados):
    alvo = normalizar(limpar(texto))
    return [termo for termo, padrao in compilados if padrao.search(alvo)]


def termos_que_batem(item, compilados):
    texto = normalizar(" ".join(
        limpar(item.get(campo))
        for campo in ("title", "titulo", "subTitulo", "content", "artType", "hierarchyStr")
    ))
    return [termo for termo, padrao in compilados if padrao.search(texto)]


# ----------------------------------------------------------------------------
# Download e extração do JSON embutido na página
# ----------------------------------------------------------------------------

def extrair_json_array(pagina):
    """Acha o jsonArray dentro do HTML do in.gov.br (tenta duas estratégias)."""
    achado = re.search(
        r'<script[^>]*\bid=["\']params["\'][^>]*>(.*?)</script>',
        pagina, flags=re.DOTALL | re.IGNORECASE,
    )
    if achado:
        try:
            dados = json.loads(achado.group(1))
            if isinstance(dados, dict) and isinstance(dados.get("jsonArray"), list):
                return dados["jsonArray"]
        except json.JSONDecodeError:
            pass

    inicio = re.search(r'"jsonArray"\s*:\s*(\[)', pagina)
    if inicio:
        try:
            lista, _ = json.JSONDecoder().raw_decode(pagina, inicio.start(1))
            if isinstance(lista, list):
                return lista
        except json.JSONDecodeError:
            pass

    raise ErroDOU(
        "não encontrei a lista de matérias na página (o site pode ter mudado "
        "a estrutura ou bloqueado o acesso automático)"
    )


def baixar_secao(dia, secao):
    """Baixa e devolve a lista de matérias (dicts) de uma seção num dia."""
    params = {"data": dia.strftime("%d-%m-%Y"), "secao": secao}
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resp = requests.get(URL_LEITURA, params=params, headers=HEADERS, timeout=90)
            resp.raise_for_status()
            return extrair_json_array(resp.text)
        except (requests.RequestException, ErroDOU) as e:
            ultimo_erro = e
            if tentativa < TENTATIVAS:
                time.sleep(5 * tentativa)
    raise ErroDOU(f"{secao.upper()} de {dia:%d/%m/%Y}: {ultimo_erro}")


# ----------------------------------------------------------------------------
# Montagem das publicações
# ----------------------------------------------------------------------------

def data_iso(texto, padrao):
    """Converte 'dd/mm/aaaa' em 'aaaa-mm-dd'; se não der, usa o padrão."""
    m = re.match(r"^\s*(\d{2})/(\d{2})/(\d{4})", str(texto or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else padrao


def montar_pub(item, secao, dia, palavras, hoje):
    url_title = (item.get("urlTitle") or "").strip()
    titulo = limpar(item.get("title") or item.get("titulo") or "")
    tipo = limpar(item.get("artType"))
    hierarquia = limpar(item.get("hierarchyStr"))
    if not titulo:
        titulo = tipo or url_title or "Matéria sem título"

    chave = url_title or f"{secao}|{dia.isoformat()}|{titulo}"
    return {
        "id": "dou-" + hashlib.md5(chave.encode("utf-8")).hexdigest()[:12],
        "fonte": "DOU",
        "titulo": titulo,
        "tipo": tipo,
        "orgao": hierarquia.split("/")[0].strip() if hierarquia else "",
        "hierarquia": hierarquia,
        "resumo": limpar(item.get("content"))[:MAX_RESUMO],
        "url": URL_MATERIA + url_title if url_title else "",
        "secao": secao.upper(),
        "edicao": item.get("editionNumber") or "",
        "pagina": item.get("numberPage") or "",
        "data_publicacao": data_iso(item.get("pubDate"), dia.isoformat()),
        "first_seen_at": hoje.isoformat(),
        "palavras": palavras,
    }



# ----------------------------------------------------------------------------
# Fonte 2: Notas Técnicas do Ministério da Saúde
# ----------------------------------------------------------------------------

def normalizar_url_nota(url):
    """Tira /view e /@@download/... para a mesma nota ter sempre o mesmo endereço-base."""
    return re.sub(r"/(view|@@download/.*)$", "", url.split("#")[0].split("?")[0])


def baixar_notas(ano, inicio):
    """HTML da listagem de notas técnicas de um ano (30 por página). None se a pasta do ano não existe (404)."""
    url = URL_NOTAS_MS.format(ano=ano)
    if inicio:
        url += f"?b_start:int={inicio}"
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=90)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            ultimo_erro = e
            if tentativa < TENTATIVAS:
                time.sleep(5 * tentativa)
    raise ErroDOU(f"Notas MS {ano} (item {inicio}): {ultimo_erro}")


def extrair_itens_notas(pagina, base_url="https://www.gov.br/"):
    """
    Lê a listagem do gov.br e devolve as notas: título, link, resumo e data.
    Não depende de classes CSS: acha os links que apontam para /notas-tecnicas/AAAA/<nota>
    e lê o texto que vem logo depois de cada um (resumo + 'publicado dd/mm/aaaa hhhmm').
    """
    ancoras = []
    for m in re.finditer(
        r'<a\b[^>]*?\bhref=["\']([^"\']*?/notas-tecnicas/\d{4}/[^"\'#?]+[^"\']*)["\'][^>]*>(.*?)</a>',
        pagina, flags=re.DOTALL | re.IGNORECASE,
    ):
        href, rotulo = m.group(1), limpar(m.group(2))
        if "@@download" in href or len(rotulo) < 8 or rotulo.lower() in ("arquivo", "leia mais", "download"):
            continue
        ancoras.append((m.start(), m.end(), urljoin(base_url, href), rotulo))

    itens, vistos = [], set()
    for i, (ini, fim, href, titulo) in enumerate(ancoras):
        chave = normalizar_url_nota(href)
        if chave in vistos:
            continue
        vistos.add(chave)
        prox = ancoras[i + 1][0] if i + 1 < len(ancoras) else len(pagina)
        texto = limpar(pagina[fim:min(prox, fim + 20000)])
        data = None
        resumo = ""
        achou = re.search(r"publicado\s*(?:em)?\s*(\d{2})/(\d{2})/(\d{4})", texto, flags=re.IGNORECASE)
        if achou:
            resumo = texto[:achou.start()].strip()
            try:
                data = date(int(achou.group(3)), int(achou.group(2)), int(achou.group(1)))
            except ValueError:
                data = None
        itens.append({"titulo": titulo, "url": href, "resumo": resumo[:MAX_RESUMO], "data": data})
    return itens


def montar_pub_nota(item, palavras, hoje):
    titulo = item["titulo"]
    tipo = re.split(r"\s+n[ºo°]", titulo, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    unidade = ""
    achou = re.search(r"\d{4}\s*[-–/]?\s*(.+)$", titulo)
    if achou and "/" in achou.group(1):
        unidade = achou.group(1).strip(" -–/")
    publicada = item["data"] or hoje
    return {
        "id": "nms-" + hashlib.md5(normalizar_url_nota(item["url"]).encode("utf-8")).hexdigest()[:12],
        "fonte": "Notas MS",
        "titulo": titulo,
        "tipo": tipo if tipo != titulo else "Nota",
        "orgao": "Ministério da Saúde",
        "hierarquia": unidade,
        "detalhe": unidade,
        "resumo": item["resumo"],
        "url": item["url"],
        "secao": "",
        "edicao": "",
        "pagina": "",
        "data_publicacao": publicada.isoformat(),
        "first_seen_at": min(publicada, hoje).isoformat(),
        "palavras": palavras,
    }


def coletar_notas_ms(cfg, hoje, compilados, res):
    """Notas publicadas nos últimos `janela_dias`. Sem filtro de palavras-chave por padrão."""
    janela = int(cfg.get("janela_dias", 15))
    max_paginas = int(cfg.get("max_paginas", 3))
    so_com_palavras = bool(cfg.get("so_com_palavras_chave", False))
    limite = hoje - timedelta(days=janela)
    anos = [hoje.year] + ([hoje.year - 1] if limite.year < hoje.year else [])

    print(f"Notas MS: últimos {janela} dias" + (" (só com palavras-chave)" if so_com_palavras else " (todas)"))
    for ano in anos:
        for pagina in range(max_paginas):
            inicio = pagina * ITENS_POR_PAGINA_NOTAS
            try:
                html_pagina = baixar_notas(ano, inicio)
            except ErroDOU as e:
                print(f"  [erro] {e}", file=sys.stderr)
                res["erros"].append(str(e))
                break
            if html_pagina is None:
                if ano == hoje.year and hoje.month == 1:
                    print(f"  Notas MS {ano}: pasta do ano ainda não existe (normal em janeiro)")
                else:
                    msg = f"Notas MS {ano}: página não encontrada (404)"
                    print(f"  [erro] {msg}", file=sys.stderr)
                    res["erros"].append(msg)
                break
            itens = extrair_itens_notas(html_pagina)
            if not itens:
                msg = f"Notas MS {ano}: não encontrei nenhuma nota na página (o site pode ter mudado)"
                print(f"  [erro] {msg}", file=sys.stderr)
                res["erros"].append(msg)
                break

            res["sucessos"] += 1
            res["varridas"] += len(itens)
            coletadas = 0
            for item in itens:
                if item["data"] and item["data"] < limite:
                    continue
                palavras = termos_no_texto(item["titulo"] + " " + item["resumo"], compilados)
                if so_com_palavras and not palavras:
                    continue
                coletadas += 1
                res["achados"].append(montar_pub_nota(item, palavras, hoje))
            print(f"  Notas MS {ano} (item {inicio}): {len(itens)} notas na página, {coletadas} na janela")

            datas = [i["data"] for i in itens if i["data"]]
            if datas and min(datas) < limite:
                break

# ----------------------------------------------------------------------------
# Classificação por regras (sem IA): nível, motivo, tema e impacto
# ----------------------------------------------------------------------------

# Secretarias do Ministério da Saúde (siglas que aparecem no fim do número da nota).
SIGLAS_MS = {
    "SAPS": "Atenção Primária",
    "SAES": "Atenção Especializada",
    "SVSA": "Vigilância em Saúde e Ambiente",
    "SESAI": "Saúde Indígena",
    "SECTICS": "Ciência e Tecnologia em Saúde",
    "SGTES": "Trabalho e Educação na Saúde",
    "SEIDIGI": "Informação e Saúde Digital",
}

# Palavras que costumam indicar que a publicação exige alguma ação/prazo de quem gere o município.
SINAIS_DE_ACAO = [
    "prazo", "adesão", "habilitação", "incentivo", "repasse", "financiamento",
    "suspensão", "prorrogação", "prorroga", "revoga", "revogação", "obrigatório",
    "credenciamento", "layout", "envio de dados",
]

TEXTO_SEM_MOTIVO = "Não foi possível confirmar município, abrangência geral ou programa específico aplicável."
TEXTO_SEM_IMPACTO = "Impacto municipal não identificado automaticamente."


def preparar_classificacao(config):
    municipio = (config.get("municipio") or "").strip()
    return {
        "municipio": municipio,
        "municipio_re": compilar_termos([municipio]) if municipio else [],
        "prioritarios": compilar_termos(config.get("termos_prioritarios", [])),
        "unidades": [u.upper() for u in config.get("unidades_de_interesse", ["SAPS"])],
        "sinais": compilar_termos(SINAIS_DE_ACAO),
    }


def gerar_tema(resumo, limite=80):
    """Etiqueta curta do assunto: início da ementa, cortado em fim de palavra."""
    texto = re.sub(r"^\s*trata-se\s+(?:de|da|do|das|dos)\s+", "", resumo or "", flags=re.IGNORECASE)
    texto = re.split(r"(?<=[.;])\s", texto.strip(), maxsplit=1)[0].rstrip(".;: ")
    if not texto:
        return ""
    if len(texto) > limite:
        texto = texto[:limite].rsplit(" ", 1)[0].rstrip(",;:- ") + "…"
    return texto[0].upper() + texto[1:]


def _unicos(lista):
    vistos, saida = set(), []
    for item in lista:
        if normalizar(item) not in vistos:
            vistos.add(normalizar(item))
            saida.append(item)
    return saida


def classificar(pub, cls):
    """Devolve nivel (alto/medio/baixo), motivo, impacto e tema, só com regras simples e visíveis."""
    texto = normalizar(" ".join([pub.get("titulo", ""), pub.get("resumo", ""), pub.get("tipo", ""),
                                 pub.get("hierarquia", ""), pub.get("detalhe", "")]))
    cita_municipio = any(rx.search(texto) for _, rx in cls["municipio_re"])
    prioritarios = [t for t, rx in cls["prioritarios"] if rx.search(texto)]
    tokens = {t.upper() for t in re.split(r"[/\s,;-]+", pub.get("detalhe", "")) if t}
    unidades = [u for u in cls["unidades"] if u in tokens]
    palavras = [w for w in pub.get("palavras", []) if normalizar(w) != normalizar(cls["municipio"])]
    citados = _unicos(prioritarios + palavras)

    if cita_municipio or prioritarios:
        nivel = "alto"
    elif citados or unidades:
        nivel = "medio"
    else:
        nivel = "baixo"

    partes = []
    if cita_municipio:
        partes.append(f"Cita o município ({cls['municipio']})")
    if citados:
        partes.append("Cita " + ", ".join(citados))
    for u in unidades:
        partes.append(f"Assinada pela {u} ({SIGLAS_MS.get(u, u)})")
    motivo = "; ".join(partes) + "." if partes else TEXTO_SEM_MOTIVO

    sinais = _unicos([t for t, rx in cls["sinais"] if rx.search(texto)])
    if nivel == "baixo":
        impacto = TEXTO_SEM_IMPACTO
    else:
        frases = []
        if cita_municipio:
            frases.append("Cita diretamente o município.")
        if sinais:
            frases.append("Menciona " + ", ".join(sinais[:4]) + " — pode haver prazo ou ação para o município.")
        elif cita_municipio:
            frases.append("Leia a publicação para ver o que se aplica.")
        else:
            tema = ", ".join(citados) if citados else ", ".join(SIGLAS_MS.get(u, u) for u in unidades)
            frases.append(f"Relacionada a {tema}; confira na publicação se afeta rotinas, prazos ou repasses do município.")
        impacto = " ".join(frases)

    return {"nivel": nivel, "motivo": motivo, "impacto": impacto, "tema": gerar_tema(pub.get("resumo", ""))}


# ----------------------------------------------------------------------------
# Arquivos
# ----------------------------------------------------------------------------

def ler_json(caminho, padrao):
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    return padrao


def gravar_json(caminho, dados):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
        f.write("\n")


def dias_para_varrer(hoje, alvo=None, retroativos=1):
    """Hoje e ontem (só dias úteis; o DOU praticamente não circula no fim de semana)."""
    if alvo:
        return [alvo]
    dias = [hoje - timedelta(days=n) for n in range(retroativos + 1)]
    return [d for d in dias if d.weekday() < 5]


# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------

def coletar_dou(config, hoje, alvo, compilados, res):
    secoes = [x.lower() for x in config.get("secoes", ["do1"])]
    retroativos = int(config.get("dias_retroativos", 1))
    dias = dias_para_varrer(hoje, alvo, retroativos)
    print(f"DOU: seções {', '.join(x.upper() for x in secoes)} | datas "
          f"{', '.join(d.strftime('%d/%m/%Y') for d in dias) or '(nenhuma: fim de semana)'}")

    for dia in dias:
        for secao in secoes:
            try:
                itens = baixar_secao(dia, secao)
            except ErroDOU as e:
                print(f"  [erro] {e}", file=sys.stderr)
                res["erros"].append("DOU " + str(e))
                continue

            res["sucessos"] += 1
            res["varridas"] += len(itens)
            batidas = 0
            for item in itens:
                palavras = termos_que_batem(item, compilados)
                if palavras:
                    batidas += 1
                    res["achados"].append(montar_pub(item, secao, dia, palavras, hoje))
            print(f"  {secao.upper()} {dia:%d/%m/%Y}: {len(itens)} matérias, {batidas} com palavras-chave")


def executar(alvo=None):
    config = ler_json(CONFIG_PATH, None)
    if not config or not config.get("termos"):
        print("config.json não encontrado ou sem 'termos'.", file=sys.stderr)
        return 1

    termos = config["termos"]
    compilados = compilar_termos(termos)
    cfg_notas = config.get("notas_ms", {})
    notas_ativas = bool(cfg_notas.get("ativo", True))

    agora = datetime.now(FUSO_BRASILIA)
    hoje = agora.date()

    dados = ler_json(DADOS_PATH, {})
    pubs = {p["id"]: p for p in dados.get("pubs", [])}

    print(f"Termos: {', '.join(termos)}")
    res = {"achados": [], "erros": [], "sucessos": 0, "varridas": 0}

    coletar_dou(config, hoje, alvo, compilados, res)
    fontes = ["DOU"]
    if notas_ativas and not alvo:
        coletar_notas_ms(cfg_notas, hoje, compilados, res)
        fontes.append("Notas MS")

    novas = 0
    for pub in res["achados"]:
        existente = pubs.get(pub["id"])
        if existente:
            antigas = existente.get("palavras", [])
            existente["palavras"] = antigas + [w for w in pub["palavras"] if w not in antigas]
        else:
            pubs[pub["id"]] = pub
            novas += 1

    classificacao = preparar_classificacao(config)
    for pub in pubs.values():
        pub.update(classificar(pub, classificacao))

    erros = res["erros"]
    if erros and res["sucessos"] == 0:
        status = "erro"
    elif erros:
        status = "parcial"
    else:
        status = "concluida"

    lista = sorted(
        pubs.values(),
        key=lambda p: (p.get("first_seen_at", ""), p.get("data_publicacao", "")),
        reverse=True,
    )[:MAX_PUBS]

    execucao = {
        "id": "r" + agora.strftime("%Y%m%d%H%M%S"),
        "started_at": agora.isoformat(timespec="seconds"),
        "status": status,
        "fontes": fontes,
        "varridas": res["varridas"],
        "total_encontradas": len(res["achados"]),
        "novas": novas,
        "erro": " | ".join(erros) if erros else None,
    }

    gravar_json(DADOS_PATH, {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "keywords": termos,
        "fontes": fontes,
        "secoes": [x.upper() for x in config.get("secoes", ["do1"])],
        "pubs": lista,
        "runs": ([execucao] + dados.get("runs", []))[:MAX_RUNS],
    })

    print(f"\nStatus: {status} | varridas: {res['varridas']} | encontradas: {len(res['achados'])} | novas: {novas}")
    return 0 if status == "concluida" else 1


def main():
    parser = argparse.ArgumentParser(description="Varredura do DOU por palavras-chave")
    parser.add_argument("--data", help="data específica no formato dd-mm-aaaa (padrão: hoje)")
    args = parser.parse_args()

    alvo = None
    if args.data:
        try:
            alvo = datetime.strptime(args.data, "%d-%m-%Y").date()
        except ValueError:
            print("Formato de data inválido. Use dd-mm-aaaa, ex.: 22-09-2026", file=sys.stderr)
            return 2

    return executar(alvo)


if __name__ == "__main__":
    sys.exit(main())
