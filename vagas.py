#!/usr/bin/env python3
"""
Radar de Vagas — encontra vagas de estágio/júnior em Dados & IA e avisa no Telegram.

Fontes:
  - Gupy (portal público de vagas, sem autenticação)
  - Greenhouse / Lever (opcional, por empresa)

Uso:
  python vagas.py                 # execução normal (busca, filtra, notifica, salva histórico)
  python vagas.py --dry-run       # só mostra no terminal; não envia nem salva
  python vagas.py --test-telegram # manda uma mensagem de teste
  python vagas.py --get-chat-id   # mostra o chat_id de quem mandou mensagem pro bot

Variáveis de ambiente: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.yaml"
SEEN_PATH = BASE / "data" / "seen.json"

# A Gupy desativou portal.api.gupy.io e employability-portal.gupy.io (out/2026).
# Endpoint atual do portal de candidatos (interno, não documentado — pode mudar de novo).
GUPY_ENDPOINTS = [
    "https://portal.gupy.io/api/job-search/jobs",
]
GUPY_PAGE = 50
GUPY_MAX_PAGES = 4

HEADERS = {"User-Agent": "radar-vagas/1.0 (uso pessoal; github.com)"}
TIMEOUT = 25

TIPOS_GUPY = {
    "vacancy_type_internship": "Estágio",
    "vacancy_type_trainee": "Trainee",
    "vacancy_type_effective": "Efetivo",
    "vacancy_type_apprentice": "Aprendiz",
    "vacancy_type_temporary": "Temporário",
    "vacancy_type_talent_pool": "Banco de talentos",
    "vacancy_type_associate": "Associado",
    "vacancy_type_freelancer": "Freelancer",
    "vacancy_type_autonomous": "Autônomo",
    "vacancy_type_lecturer": "Docente",
    "vacancy_type_volunteer": "Voluntário",
    "vacancy_legal_entity": "PJ",
    "vacancy_type_summer": "Summer job",
}
TIPOS_NIVEL_OK = {"vacancy_type_internship", "vacancy_type_trainee", "vacancy_type_summer"}
TIPOS_DESCARTE = {"vacancy_type_talent_pool", "vacancy_type_apprentice"}

MODALIDADE_LABEL = {"remote": "🌎 Remoto", "hybrid": "🏙️ Híbrido", "on-site": "🏢 Presencial"}


# ------------------------------------------------------------------ utilidades

def norm(texto: str | None) -> str:
    """minúsculas, sem acento, só letras/números separados por espaço."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return f" {t.strip()} "


def compila(termos: list[str]) -> list[tuple[str, re.Pattern]]:
    """Cada termo casa como palavra/expressão inteira ('ia' não casa com 'media').
    Termo terminado em '*' casa como prefixo ('estagi*' -> estagio, estagiario)."""
    out = []
    for termo in termos or []:
        prefixo = str(termo).strip().endswith("*")
        n = norm(str(termo).rstrip("* ")).strip()
        if not n:
            continue
        esc = re.escape(n).replace(r"\ ", r"\s+")
        fim = "" if prefixo else r"(?![a-z0-9])"
        out.append((termo, re.compile(rf"(?<![a-z0-9]){esc}{fim}")))
    return out


def bate(padroes, texto_norm: str) -> list[str]:
    return [termo for termo, p in padroes if p.search(texto_norm)]


def parse_data(valor) -> datetime | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):  # epoch em ms (Lever)
        return datetime.fromtimestamp(valor / 1000, tz=timezone.utc)
    try:
        s = str(valor).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_json(url: str, params: dict | None = None, tentativas: int = 3):
    ultimo_erro = None
    for i in range(tentativas):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            if 400 <= r.status_code < 500:
                # erro do cliente (404 etc.) não melhora tentando de novo
                raise RuntimeError(f"HTTP {r.status_code} em {url}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            ultimo_erro = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"falha em {url}: {ultimo_erro}")


# ------------------------------------------------------------------ modelo

@dataclass
class Vaga:
    uid: str
    titulo: str
    empresa: str
    url: str
    fonte: str
    publicada: datetime | None = None
    modalidade: str = ""          # remote | hybrid | on-site | ""
    local: str = ""
    tipo: str = ""                # código do tipo (Gupy)
    descricao: str = ""
    marcas: list[str] = field(default_factory=list)

    @property
    def tipo_label(self) -> str:
        return TIPOS_GUPY.get(self.tipo, "")


# ------------------------------------------------------------------ fontes

def _primeiro(j: dict, *chaves):
    """Devolve o primeiro campo preenchido entre várias grafias possíveis."""
    for c in chaves:
        v = j.get(c)
        if isinstance(v, dict):
            v = v.get("name") or v.get("title")
        if v not in (None, "", []):
            return v
    return None


def _local_gupy(j: dict) -> str:
    cidade = _primeiro(j, "city", "cityName")
    estado = _primeiro(j, "state", "stateName", "uf")
    loc = j.get("location") or j.get("address")
    if isinstance(loc, dict):
        cidade = cidade or _primeiro(loc, "city", "cityName")
        estado = estado or _primeiro(loc, "state", "stateName", "uf")
    elif isinstance(loc, str) and not (cidade or estado):
        return loc
    return ", ".join(str(x) for x in (cidade, estado) if x)


def buscar_gupy(termos: list[str], log, debug: bool = False) -> list[Vaga]:
    vagas: dict[str, Vaga] = {}
    endpoint = None
    falhas = 0
    for termo in termos:
        if falhas >= 2:
            log("  ! Gupy fora do ar — pulando o resto das buscas")
            break
        for pagina in range(GUPY_MAX_PAGES):
            params = {"jobName": termo, "limit": GUPY_PAGE, "offset": pagina * GUPY_PAGE}
            dados, erros = None, []
            for ep in ([endpoint] if endpoint else GUPY_ENDPOINTS):
                try:
                    dados = get_json(ep, params)
                    endpoint = ep
                    break
                except RuntimeError as e:
                    erros.append(str(e))
            if dados is None:
                log(f"  ! Gupy falhou em '{termo}': " + " | ".join(erros))
                falhas += 1
                break
            falhas = 0
            itens = dados.get("data", []) if isinstance(dados, dict) else (dados or [])
            if debug and itens:
                log("---- exemplo bruto de vaga da Gupy ----")
                log(json.dumps(itens[0], ensure_ascii=False, indent=2)[:3000])
                debug = False
            for j in itens:
                jid = j.get("id")
                if jid is None:
                    continue
                uid = f"gupy:{jid}"
                if uid in vagas:
                    continue
                modalidade = (j.get("workplaceType") or "").lower()
                if not modalidade and j.get("isRemoteWork"):
                    modalidade = "remote"
                vagas[uid] = Vaga(
                    uid=uid,
                    titulo=str(_primeiro(j, "name", "title") or "").strip(),
                    empresa=str(_primeiro(j, "careerPageName", "companyName", "company") or "").strip(),
                    url=str(_primeiro(j, "url", "jobUrl", "link") or ""),
                    fonte="Gupy",
                    publicada=parse_data(_primeiro(j, "publishedDate", "publishedAt", "createdAt")),
                    modalidade=modalidade.replace("onsite", "on-site").replace("presencial", "on-site"),
                    local=_local_gupy(j),
                    tipo=str(_primeiro(j, "type", "jobType") or ""),
                    descricao=str(_primeiro(j, "description", "descriptionText") or ""),
                )
            if len(itens) < GUPY_PAGE:
                break
            time.sleep(0.4)
        time.sleep(0.4)
    log(f"Gupy: {len(vagas)} vagas únicas nas buscas")
    return list(vagas.values())


def _modalidade_por_texto(texto: str) -> str:
    n = norm(texto)
    if re.search(r"\b(remote|remoto|anywhere|home office)\b", n):
        return "remote"
    if re.search(r"\b(hybrid|hibrido)\b", n):
        return "hybrid"
    return "on-site" if n.strip() else ""


def buscar_greenhouse(empresas: list[str], log) -> list[Vaga]:
    out = []
    for emp in empresas or []:
        try:
            dados = get_json(f"https://boards-api.greenhouse.io/v1/boards/{emp}/jobs")
        except RuntimeError as e:
            log(f"  ! Greenhouse {emp}: {e}")
            continue
        for j in dados.get("jobs", []):
            local = (j.get("location") or {}).get("name", "")
            out.append(Vaga(
                uid=f"gh:{emp}:{j.get('id')}",
                titulo=j.get("title", ""),
                empresa=emp,
                url=j.get("absolute_url", ""),
                fonte="Greenhouse",
                publicada=parse_data(j.get("first_published") or j.get("updated_at")),
                modalidade=_modalidade_por_texto(local),
                local=local,
            ))
    if empresas:
        log(f"Greenhouse: {len(out)} vagas")
    return out


def buscar_lever(empresas: list[str], log) -> list[Vaga]:
    out = []
    for emp in empresas or []:
        try:
            dados = get_json(f"https://api.lever.co/v0/postings/{emp}", {"mode": "json"})
        except RuntimeError as e:
            log(f"  ! Lever {emp}: {e}")
            continue
        for j in dados if isinstance(dados, list) else []:
            cat = j.get("categories") or {}
            local = cat.get("location", "") or ""
            wp = (j.get("workplaceType") or "").lower().replace("onsite", "on-site")
            out.append(Vaga(
                uid=f"lever:{emp}:{j.get('id')}",
                titulo=j.get("text", ""),
                empresa=emp,
                url=j.get("hostedUrl", ""),
                fonte="Lever",
                publicada=parse_data(j.get("createdAt")),
                modalidade=wp or _modalidade_por_texto(local),
                local=local,
                descricao=j.get("descriptionPlain", "") or "",
            ))
    if empresas:
        log(f"Lever: {len(out)} vagas")
    return out


# ------------------------------------------------------------------ filtro

class Filtro:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.area = compila(cfg.get("area"))
        self.nivel = compila(cfg.get("nivel"))
        self.excluir = compila(cfg.get("excluir"))
        self.programa = compila(cfg.get("termos_programa"))
        self.area_desc = compila(cfg.get("area_descricao"))
        self.locais = [norm(x) for x in cfg.get("locais_presencial_hibrido") or []]
        self.max_dias = cfg.get("max_dias_publicacao")

    def avalia(self, v: Vaga, agora: datetime) -> tuple[bool, str]:
        t = norm(v.titulo)

        if v.tipo in TIPOS_DESCARTE:
            return False, f"tipo {v.tipo_label}"
        if (m := bate(self.excluir, t)):
            return False, f"excluído ({m[0]})"

        if self.max_dias and v.publicada and v.publicada < agora - timedelta(days=self.max_dias):
            return False, "antiga"

        # nível
        nivel_ok = v.tipo in TIPOS_NIVEL_OK or bool(bate(self.nivel, t))
        if not nivel_ok:
            return False, "nível"

        # área
        if not bate(self.area, t):
            generico = self.cfg.get("aceitar_programas_genericos") and bate(self.programa, t)
            if not generico:
                return False, "área"
            achados = set(bate(self.area_desc, norm(v.descricao)))
            if len(achados) < int(self.cfg.get("min_termos_descricao", 2)):
                return False, "área (programa genérico sem dados/IA na descrição)"
            v.marcas.append("programa geral — confira a trilha de dados/IA")

        # modalidade / local
        mod = v.modalidade
        if mod == "remote":
            if not self.cfg.get("aceitar_remoto", True):
                return False, "remoto desativado"
        else:
            if mod == "hybrid" and not self.cfg.get("aceitar_hibrido", True):
                return False, "híbrido desativado"
            if mod == "on-site" and not self.cfg.get("aceitar_presencial", True):
                return False, "presencial desativado"
            if self.locais:
                loc = norm(v.local)
                if not any(l.strip() and l in loc for l in self.locais):
                    return False, f"local ({v.local or '?'})"
        return True, "ok"


# ------------------------------------------------------------------ histórico

def carregar_vistos() -> dict[str, str] | None:
    if not SEEN_PATH.exists():
        return None
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def salvar_vistos(vistos: dict[str, str], dias: int):
    limite = (datetime.now(timezone.utc) - timedelta(days=dias)).date().isoformat()
    vistos = {k: d for k, d in vistos.items() if d >= limite}
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(vistos, indent=0, sort_keys=True), encoding="utf-8")


# ------------------------------------------------------------------ Telegram

class Telegram:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token, self.chat_id = token, chat_id

    @property
    def ativo(self) -> bool:
        return bool(self.token and self.chat_id)

    def enviar(self, texto: str):
        r = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            espera = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(espera + 1)
            return self.enviar(texto)
        if not r.ok:
            raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")
        time.sleep(1.1)


def formatar(v: Vaga) -> str:
    e = html.escape
    linha2 = [f"🏛️ {e(v.empresa or v.fonte)}"]
    if v.modalidade in MODALIDADE_LABEL:
        linha2.append(MODALIDADE_LABEL[v.modalidade])
    if v.local and v.modalidade != "remote":
        linha2.append(e(v.local))
    partes = [f"<b>{e(v.titulo)}</b>", " · ".join(linha2)]
    extra = []
    if v.tipo_label:
        extra.append(f"🎓 {v.tipo_label}")
    if v.publicada:
        extra.append(f"📅 {v.publicada.astimezone(timezone(timedelta(hours=-3))):%d/%m}")
    extra.append(v.fonte)
    partes.append(" · ".join(extra))
    for m in v.marcas:
        partes.append(f"⚠️ <i>{e(m)}</i>")
    partes.append(f'<a href="{e(v.url, quote=True)}">Ver vaga →</a>')
    return "\n".join(partes)


def agrupar(blocos: list[str], cabecalho: str, limite: int = 3800) -> list[str]:
    msgs, atual = [], cabecalho
    for b in blocos:
        if len(atual) + len(b) + 2 > limite:
            msgs.append(atual)
            atual = ""
        atual += ("\n\n" if atual else "") + b
    if atual:
        msgs.append(atual)
    return msgs


# ------------------------------------------------------------------ principal

def ordenar(vagas: list[Vaga]) -> list[Vaga]:
    prio = {"remote": 0, "hybrid": 1, "on-site": 2}
    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(vagas, key=lambda v: (prio.get(v.modalidade, 3), -(v.publicada or min_dt).timestamp()))


def main():
    ap = argparse.ArgumentParser(description="Radar de vagas de estágio/júnior em Dados & IA")
    ap.add_argument("--dry-run", action="store_true", help="mostra no terminal; não envia nem salva")
    ap.add_argument("--verbose", "-v", action="store_true", help="mostra o motivo de cada descarte")
    ap.add_argument("--debug-gupy", action="store_true", help="mostra uma vaga crua da Gupy (para checar campos)")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--get-chat-id", action="store_true")
    args = ap.parse_args()

    log = lambda msg: print(msg, file=sys.stderr)
    tg = Telegram(os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))

    if args.get_chat_id:
        if not tg.token:
            sys.exit("Defina TELEGRAM_TOKEN primeiro.")
        upd = requests.get(f"https://api.telegram.org/bot{tg.token}/getUpdates", timeout=TIMEOUT).json()
        chats = {(u.get("message") or {}).get("chat", {}).get("id") for u in upd.get("result", [])}
        chats.discard(None)
        print("\n".join(map(str, chats)) or "Nenhuma mensagem. Mande um 'oi' pro bot e rode de novo.")
        return

    if args.test_telegram:
        if not tg.ativo:
            sys.exit("Defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
        tg.enviar("✅ <b>Radar de Vagas</b> conectado! Você vai receber as vagas novas por aqui.")
        print("Mensagem de teste enviada.")
        return

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    agora = datetime.now(timezone.utc)

    todas = (
        buscar_gupy(cfg.get("gupy_termos") or [], log, debug=args.debug_gupy)
        + buscar_greenhouse(cfg.get("greenhouse") or [], log)
        + buscar_lever(cfg.get("lever") or [], log)
    )
    if not todas:
        log("Nenhuma vaga retornada pelas fontes — possível falha de rede/API. Nada foi alterado.")
        sys.exit(1)

    filtro = Filtro(cfg)
    aprovadas, motivos = [], {}
    for v in todas:
        ok, motivo = filtro.avalia(v, agora)
        if ok:
            aprovadas.append(v)
        else:
            motivos[motivo.split(" (")[0]] = motivos.get(motivo.split(" (")[0], 0) + 1
            if args.verbose:
                log(f"  ✗ [{motivo}] {v.titulo} — {v.empresa}")
    log(f"Aprovadas no filtro: {len(aprovadas)} | descartes: {motivos}")

    vistos = carregar_vistos()
    primeira = vistos is None
    vistos = vistos or {}
    novas = ordenar([v for v in aprovadas if v.uid not in vistos])
    log(f"Novas (não vistas antes): {len(novas)}")

    if args.dry_run:
        for v in novas:
            print("\n" + re.sub(r"<[^>]+>", "", formatar(v)) + f"\n{v.url}")
        print(f"\n[dry-run] {len(novas)} vagas novas. Nada enviado/salvo.")
        return

    hoje = agora.date().isoformat()
    max_n = int(cfg.get("max_por_execucao", 25))

    if not tg.ativo:
        log("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID ausentes — mostrando no terminal SEM salvar histórico.")
        for v in novas:
            print("\n" + re.sub(r"<[^>]+>", "", formatar(v)) + f"\n{v.url}")
        sys.exit(2 if os.getenv("GITHUB_ACTIONS") else 0)

    if primeira and cfg.get("primeira_execucao_silenciosa", True):
        destaque = novas[:5]
        blocos = [formatar(v) for v in destaque]
        cab = (f"🛰️ <b>Radar de Vagas ativado!</b>\nEncontrei {len(novas)} vagas abertas que batem "
               f"com seus filtros. A partir de agora só aviso as <b>novas</b>.\n"
               f"Algumas para começar:")
        for msg in agrupar(blocos, cab):
            tg.enviar(msg)
    elif novas:
        enviar, sobra = novas[:max_n], novas[max_n:]
        cab = f"🛰️ <b>{len(novas)} vaga{'s' if len(novas) > 1 else ''} nova{'s' if len(novas) > 1 else ''}</b> em Dados & IA"
        for msg in agrupar([formatar(v) for v in enviar], cab):
            tg.enviar(msg)
        if sobra:
            tg.enviar(f"…e mais {len(sobra)} vagas. Aperte os filtros no config.yaml se estiver vindo muita coisa.")

    for v in novas:
        vistos[v.uid] = hoje
    salvar_vistos(vistos, int(cfg.get("dias_memoria", 90)))
    log("Histórico salvo.")


if __name__ == "__main__":
    main()
