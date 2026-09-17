"""Servidor MCP da central de salas (Streamable HTTP, revisao 2026-07-28).

Tres tools, um resource e o ciclo completo de MRTR na reserva. O `requestState`
e selado pelo proprio SDK (AES-256-GCM sob chave derivada por HKDF do segredo
em REQUEST_STATE_SECRET), entao um retry sobrevive a um restart do processo.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

from dominio import ERRO_SEM_ALTERNATIVA, Agenda, ErroDeNegocio, ler_politica, versao_da_politica

NOME = "central-de-salas"
VERSAO = "1.0.0"
# Entre 5 e 30 minutos, como o enunciado exige. 15 minutos.
TTL_REQUEST_STATE = 900.0

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("central-de-salas")

AGENDA = Agenda()


def _segredo() -> str:
    valor = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(valor.encode()) < 32:
        raise SystemExit(
            "REQUEST_STATE_SECRET ausente ou curto demais (minimo 32 bytes).\n"
            'Gere um com: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return valor


# ---------------------------------------------------------------- modelos de saida


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ------------------------------------------------------- log de cada request no stderr


async def registrar_no_stderr(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    """Middleware de log: metodo, id e traceparent de cada request recebido."""
    meta = (ctx.params or {}).get("_meta") or {}
    traceparent = meta.get("traceparent")
    alvo = (ctx.params or {}).get("name") or (ctx.params or {}).get("uri") or ""
    log.info(
        "request method=%s id=%s name=%s traceparent=%s",
        ctx.method,
        ctx.request_id,
        alvo,
        traceparent if traceparent else "-",
    )
    return await call_next(ctx)


servidor = MCPServer(
    name=NOME,
    version=VERSAO,
    request_state_security=RequestStateSecurity(
        keys=[_segredo()],
        ttl=TTL_REQUEST_STATE,
        # Sem autenticacao nesta entrega: sem principal para vincular.
        bind_principal=None,
    ),
    middleware=[registrar_no_stderr],
)


# ------------------------------------------------------------------------- resolver


def _modelo_de_escolha(alternativas: list[str]) -> type[BaseModel]:
    """Modelo plano com uma propriedade `sala` restrita as alternativas."""
    return create_model(
        "EscolhaDeSala",
        sala=(
            Literal[tuple(alternativas)],  # type: ignore[valid-type]
            Field(description="Sala alternativa escolhida", title="Sala"),
        ),
    )


def escolha_de_sala(
    sala: str, inicio: str, fim: str, ctx: Context
) -> Elicit[Any] | None:
    """Resolver do MRTR.

    Roda antes do corpo da tool. Sem conflito devolve None e a reserva segue
    direto. Com conflito devolve um Elicit, e o framework termina a resposta
    com resultType=input_required, inputRequests e requestState selado.
    """
    try:
        pedida, comeco, termino = AGENDA.validar(sala, inicio, fim)
    except ErroDeNegocio:
        # A validacao e reportada pelo corpo da tool, com a mensagem exata.
        return None
    if AGENDA.livre(sala, comeco, termino):
        return None
    alternativas = AGENDA.alternativas(pedida, comeco, termino)
    if not alternativas:
        # Sem elicitation: o corpo da tool devolve o erro de execucao.
        return None
    return Elicit(
        "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        _modelo_de_escolha(alternativas),
    )


# ----------------------------------------------------------------------------- tools


@servidor.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in AGENDA.salas])


@servidor.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        _, comeco, termino = AGENDA.validar(sala, inicio, fim)
    except ErroDeNegocio as e:
        raise ToolError(str(e)) from None
    conflitos = AGENDA.conflitos(sala, comeco, termino)
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[ConflitoOut(id=c.id, inicio=c.inicio, fim=c.fim, responsavel=c.responsavel) for c in conflitos],
    )


@servidor.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)] = None,  # type: ignore[assignment]
) -> ReservaOut:
    try:
        _, comeco, termino = AGENDA.validar(sala, inicio, fim)
    except ErroDeNegocio as e:
        raise ToolError(str(e)) from None

    if isinstance(escolha, DeclinedElicitation | CancelledElicitation):
        return ReservaOut(reservado=False, motivo="recusado")

    # O resolver ja perguntou quando havia conflito com alternativa. Chegar aqui
    # com `escolha` vazio significa que a sala pedida esta livre, ou que nao havia
    # alternativa nenhuma para oferecer.
    alvo = sala
    if isinstance(escolha, AcceptedElicitation) and escolha.data is not None:
        alvo = getattr(escolha.data, "sala", sala)

    if not AGENDA.livre(alvo, comeco, termino):
        raise ToolError(ERRO_SEM_ALTERNATIVA)

    criada = AGENDA.criar(alvo, inicio, fim, responsavel)
    return ReservaOut(
        reserva=criada.id,
        reservado=True,
        sala=criada.sala,
        inicio=criada.inicio,
        fim=criada.fim,
        responsavel=criada.responsavel,
        politica=versao_da_politica(),
    )


# -------------------------------------------------------------------------- resource


@servidor.resource("politica://uso", mime_type="text/markdown", description="Politica de uso das salas.")
def politica_de_uso() -> str:
    return ler_politica()


def main() -> None:
    porta = int(os.environ.get("MCP_PORT", "7301"))
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    app = servidor.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host)
    log.info("servidor MCP em http://%s:%d/mcp", host, porta)
    uvicorn.run(app, host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
