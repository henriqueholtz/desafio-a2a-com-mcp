"""Regras de dominio da central de salas.

Todo o negocio (conflito, politica, alternativas) vive aqui, do lado do
servidor MCP. O agente nao replica nada disto: ele so traduz protocolo.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

RAIZ_DADOS = Path(__file__).resolve().parent.parent / "dados"

# Mensagens exatas exigidas pelo enunciado. Fonte unica de verdade.
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

JANELA_INICIO = 8
JANELA_FIM = 20
DURACAO_MAXIMA = timedelta(hours=2)


class ErroDeNegocio(Exception):
    """Erro de execucao da tool: vira isError: true com a mensagem exata."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


def _carregar_salas() -> list[Sala]:
    bruto = json.loads((RAIZ_DADOS / "salas.json").read_text(encoding="utf-8"))
    return [Sala(s["id"], s["nome"], s["capacidade"], list(s["recursos"])) for s in bruto]


def _carregar_reservas() -> list[Reserva]:
    bruto = json.loads((RAIZ_DADOS / "reservas.json").read_text(encoding="utf-8"))
    return [Reserva(r["id"], r["sala"], r["inicio"], r["fim"], r["responsavel"]) for r in bruto]


def ler_politica() -> str:
    return (RAIZ_DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_da_politica() -> str:
    primeira = ler_politica().splitlines()[0]
    return primeira.split(":", 1)[1].strip()


def _parse(momento: str) -> datetime:
    try:
        valor = datetime.fromisoformat(momento)
    except (TypeError, ValueError):
        raise ErroDeNegocio(ERRO_INTERVALO) from None
    if valor.tzinfo is None:
        raise ErroDeNegocio(ERRO_INTERVALO)
    return valor


class Agenda:
    """Estado das reservas em memoria. Nao persiste em disco, por desenho."""

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._salas = {s.id: s for s in _carregar_salas()}
        self._reservas = _carregar_reservas()
        self._proximo = len(self._reservas) + 1

    @property
    def salas(self) -> list[Sala]:
        return list(self._salas.values())

    def sala(self, sala_id: str) -> Sala:
        encontrada = self._salas.get(sala_id)
        if encontrada is None:
            raise ErroDeNegocio(ERRO_SALA.format(sala=sala_id))
        return encontrada

    def validar(self, sala_id: str, inicio: str, fim: str) -> tuple[Sala, datetime, datetime]:
        """Aplica sala + politica, na ordem em que o enunciado define os erros."""
        alvo = self.sala(sala_id)
        comeco = _parse(inicio)
        termino = _parse(fim)
        if termino <= comeco:
            raise ErroDeNegocio(ERRO_INTERVALO)
        if not (JANELA_INICIO <= comeco.hour < JANELA_FIM) or not self._fim_na_janela(termino):
            raise ErroDeNegocio(ERRO_JANELA)
        if termino - comeco > DURACAO_MAXIMA:
            raise ErroDeNegocio(ERRO_DURACAO)
        return alvo, comeco, termino

    @staticmethod
    def _fim_na_janela(termino: datetime) -> bool:
        if termino.hour > JANELA_FIM:
            return False
        if termino.hour == JANELA_FIM:
            return termino.minute == 0 and termino.second == 0
        return termino.hour >= JANELA_INICIO

    def conflitos(self, sala_id: str, comeco: datetime, termino: datetime) -> list[Reserva]:
        achados = []
        for r in self._reservas:
            if r.sala != sala_id:
                continue
            ri, rf = datetime.fromisoformat(r.inicio), datetime.fromisoformat(r.fim)
            if comeco < rf and ri < termino:
                achados.append(r)
        return achados

    def livre(self, sala_id: str, comeco: datetime, termino: datetime) -> bool:
        return not self.conflitos(sala_id, comeco, termino)

    def alternativas(self, pedida: Sala, comeco: datetime, termino: datetime) -> list[str]:
        """Salas livres no intervalo com capacidade >= a da pedida.

        No maximo tres, ordenadas por capacidade crescente e, no empate, por id.
        """
        candidatas = [
            s
            for s in self._salas.values()
            if s.id != pedida.id and s.capacidade >= pedida.capacidade and self.livre(s.id, comeco, termino)
        ]
        candidatas.sort(key=lambda s: (s.capacidade, s.id))
        return [s.id for s in candidatas[:3]]

    def criar(self, sala_id: str, inicio: str, fim: str, responsavel: str) -> Reserva:
        with self._trava:
            nova = Reserva(f"res-{self._proximo:04d}", sala_id, inicio, fim, responsavel)
            self._proximo += 1
            self._reservas.append(nova)
            return nova
