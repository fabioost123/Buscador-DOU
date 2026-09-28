#!/usr/bin/env python3
"""
Buscador DOU - varredura diária do Diário Oficial da União por palavras-chave.

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

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DADOS_PATH = os.path.join(BASE_DIR, "dados.json")

URL_LEITURA = "https://www.in.gov.br/leiturajornal"
URL_MATERIA = "https://www.in.gov.br/web/dou/-/"
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

def executar(alvo=None):
    config = ler_json(CONFIG_PATH, None)
    if not config or not config.get("termos"):
        print("config.json não encontrado ou sem 'termos'.", file=sys.stderr)
        return 1

    termos = config["termos"]
    secoes = [s.lower() for s in config.get("secoes", ["do1"])]
    retroativos = int(config.get("dias_retroativos", 1))
    compilados = compilar_termos(termos)

    agora = datetime.now(FUSO_BRASILIA)
    hoje = agora.date()
    dias = dias_para_varrer(hoje, alvo, retroativos)

    dados = ler_json(DADOS_PATH, {})
    pubs = {p["id"]: p for p in dados.get("pubs", [])}

    varridas = encontradas = novas = 0
    erros = []
    sucessos = 0

    print(f"Termos: {', '.join(termos)}")
    print(f"Seções: {', '.join(s.upper() for s in secoes)} | Datas: "
          f"{', '.join(d.strftime('%d/%m/%Y') for d in dias) or '(nenhuma: fim de semana)'}")

    for dia in dias:
        for secao in secoes:
            try:
                itens = baixar_secao(dia, secao)
            except ErroDOU as e:
                print(f"  [erro] {e}", file=sys.stderr)
                erros.append(str(e))
                continue

            sucessos += 1
            varridas += len(itens)
            batidas = 0
            for item in itens:
                palavras = termos_que_batem(item, compilados)
                if not palavras:
                    continue
                batidas += 1
                encontradas += 1
                pub = montar_pub(item, secao, dia, palavras, hoje)
                if pub["id"] in pubs:
                    antigas = pubs[pub["id"]].get("palavras", [])
                    pubs[pub["id"]]["palavras"] = antigas + [p for p in palavras if p not in antigas]
                else:
                    pubs[pub["id"]] = pub
                    novas += 1
            print(f"  {secao.upper()} {dia:%d/%m/%Y}: {len(itens)} matérias, {batidas} com palavras-chave")

    if erros and sucessos == 0:
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
        "secoes": [s.upper() for s in secoes],
        "datas": [d.isoformat() for d in dias],
        "varridas": varridas,
        "total_encontradas": encontradas,
        "novas": novas,
        "erro": " | ".join(erros) if erros else None,
    }

    gravar_json(DADOS_PATH, {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "keywords": termos,
        "secoes": [s.upper() for s in secoes],
        "pubs": lista,
        "runs": ([execucao] + dados.get("runs", []))[:MAX_RUNS],
    })

    print(f"\nStatus: {status} | varridas: {varridas} | encontradas: {encontradas} | novas: {novas}")
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
