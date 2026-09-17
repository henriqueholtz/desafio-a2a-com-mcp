"""Agente da central de salas: host MCP por dentro, servidor A2A por fora.

Por dentro ele descobre as tools do servidor MCP em runtime e as chama por HTTP
de verdade. Por fora ele publica um Agent Card v1.0 e atende SendMessage e
GetTask no binding JSON-RPC.

Nenhuma regra de sala vive aqui. Conflito, politica e alternativas sao decisao
do servidor MCP; este processo traduz protocolo, e so.

Sem LLM: o pedido chega em formato fixo e e interpretado por string parsing, de
modo que o mesmo pedido sempre produz o mesmo resultado.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from cliente_mcp import ClienteMCP, ErroDeProtocolo, texto_do_resultado
from tarefas import (
    CANCELED,
    COMPLETED,
    FAILED,
    INPUT_REQUIRED,
    PAPEL_USUARIO,
    WORKING,
    DepositoDeTarefas,
    Pausa,
    Task,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agente")

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}")

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"

RE_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

mcp = ClienteMCP(URL_MCP)
tarefas = DepositoDeTarefas()


# ----------------------------------------------------------------- descoberta


class Descoberta:
    """Catalogo descoberto em runtime: tools por tools/list, politica por resource.

    Nada de lista fixa no codigo. O agente so chama o que descobriu.
    """

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._tools: dict[str, dict[str, Any]] = {}
        self._versao_politica: str | None = None

    def preparar(self, trace_id: str | None) -> None:
        """Garante um tools/list e uma leitura do resource antes do primeiro tools/call."""
        with self._trava:
            if not self._tools:
                self._tools = {t["name"]: t for t in mcp.listar_tools(trace_id=trace_id)}
                log.info("tools descobertas: %s", sorted(self._tools))
            if self._versao_politica is None:
                texto = mcp.ler_resource(URI_POLITICA, trace_id=trace_id)
                self._versao_politica = texto.splitlines()[0].split(":", 1)[1].strip()
                log.info("politica versao=%s", self._versao_politica)

    def tem(self, nome: str) -> bool:
        with self._trava:
            return nome in self._tools

    @property
    def versao_politica(self) -> str:
        return self._versao_politica or ""


catalogo = Descoberta()


# ------------------------------------------------------------- parsing do texto


def _campos(texto: str) -> dict[str, str]:
    """Le pares chave=valor separados por espaco. Deterministico, sem LLM."""
    achados: dict[str, str] = {}
    for pedaco in texto.split():
        if "=" in pedaco:
            chave, valor = pedaco.split("=", 1)
            achados[chave.strip()] = valor.strip()
    return achados


def _texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [])


# ------------------------------------------------------------------- a ponte


def _pausar(tarefa: Task, resultado: dict[str, Any], argumentos: dict[str, Any]) -> None:
    """A PONTE, sentido MCP -> A2A.

    Aqui o `input_required` do servidor MCP vira TASK_STATE_INPUT_REQUIRED: a
    Task para, a lista de alternativas volta como mensagem de texto, e o
    `requestState` fica guardado ligado a esta Task (e so a ela).
    """
    pedidos = resultado.get("inputRequests") or {}
    chave = next(iter(pedidos))
    schema = (pedidos[chave].get("params") or {}).get("requestedSchema") or {}
    campo = (schema.get("properties") or {}).get("sala") or {}
    alternativas = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))

    tarefa.pausa = Pausa(
        chave=chave,
        request_state=resultado["requestState"],
        alternativas=alternativas,
        argumentos=argumentos,
        tool=TOOL_RESERVA,
    )
    # Byte a byte a linha que o enunciado fixa: sem prefixo, sem saudacao.
    tarefa.dizer(f"alternativas: {', '.join(alternativas)}", INPUT_REQUIRED)


def _retomar(tarefa: Task, acao: str, escolhida: str | None) -> None:
    """A PONTE, sentido A2A -> MCP.

    Repete o mesmo tools/call com um id de JSON-RPC novo (o ClienteMCP nunca
    reaproveita id), levando `inputResponses` com a mesma chave que veio no
    `inputRequests` e o `requestState` ecoado sem modificacao.
    """
    pausa = tarefa.pausa
    assert pausa is not None
    resposta: dict[str, Any] = {"action": acao}
    if acao == "accept":
        resposta["content"] = {"sala": escolhida}

    resultado = mcp.chamar_tool(
        pausa.tool,
        pausa.argumentos,
        input_responses={pausa.chave: resposta},
        request_state=pausa.request_state,
        trace_id=tarefa.trace_id,
    )
    _concluir(tarefa, resultado, pausa.argumentos, recusa=(acao != "accept"))


def _concluir(tarefa: Task, resultado: dict[str, Any], argumentos: dict[str, Any], *, recusa: bool = False) -> None:
    """Traduz um resultado do MCP no estado terminal (ou na pausa) da Task."""
    if resultado.get("resultType") == "input_required":
        _pausar(tarefa, resultado, argumentos)
        return

    if resultado.get("isError"):
        # A mensagem exata da tool aparece no historico da Task.
        tarefa.pausa = None
        tarefa.dizer(texto_do_resultado(resultado), FAILED)
        return

    dados = resultado.get("structuredContent") or {}
    tarefa.pausa = None

    if not dados.get("reservado"):
        motivo = dados.get("motivo") or "recusado"
        tarefa.dizer(f"Reserva nao realizada: {motivo}.", CANCELED if recusa else FAILED)
        return

    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": dados.get("politica") or catalogo.versao_politica,
    }
    tarefa.anexar("reserva", json.dumps(reserva, ensure_ascii=False))
    tarefa.dizer(f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.", COMPLETED)


# --------------------------------------------------------------- SendMessage


class ErroA2A(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def send_message(params: dict[str, Any], trace_id: str | None) -> dict[str, Any]:
    mensagem = params.get("message") or {}
    texto = _texto_da_mensagem(mensagem)
    task_id = mensagem.get("taskId") or params.get("taskId")

    if task_id:
        tarefa = tarefas.obter(task_id)
        if tarefa is None:
            raise ErroA2A(-32001, f"Task nao encontrada: {task_id}")
        if tarefa.terminal:
            # Estado terminal e definitivo.
            raise ErroA2A(-32002, f"Task em estado terminal: {tarefa.state}")
    else:
        tarefa = tarefas.nova()

    if trace_id:
        tarefa.trace_id = trace_id
    tarefa.registrar_usuario(
        {
            "messageId": mensagem.get("messageId") or "msg-desconhecido",
            "role": mensagem.get("role") or PAPEL_USUARIO,
            "parts": mensagem.get("parts") or [{"text": texto}],
            **({"taskId": tarefa.id} if task_id else {}),
        }
    )

    try:
        catalogo.preparar(tarefa.trace_id)
        if tarefa.pausa is not None:
            _responder_pausa(tarefa, texto)
        else:
            _abrir(tarefa, texto)
    except ErroDeProtocolo as e:
        tarefa.pausa = None
        tarefa.dizer(f"Erro de protocolo do servidor MCP: {e}", FAILED)
    except ErroA2A:
        raise
    except Exception as e:  # noqa: BLE001 - a Task precisa terminar em algum estado
        log.exception("falha tratando a Task %s", tarefa.id)
        tarefa.pausa = None
        tarefa.dizer(f"Falha interna: {e}", FAILED)

    return {"task": tarefa.para_wire()}


def _abrir(tarefa: Task, texto: str) -> None:
    """Primeiro SendMessage da Task: interpreta o pedido e chama o MCP."""
    campos = _campos(texto)
    if not texto.strip().startswith("reservar") or not {"sala", "inicio", "fim", "responsavel"} <= set(campos):
        tarefa.dizer(
            "Pedido nao reconhecido. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
            FAILED,
        )
        return

    if not catalogo.tem(TOOL_RESERVA):
        tarefa.dizer(f"O servidor MCP nao expoe a tool {TOOL_RESERVA}.", FAILED)
        return

    argumentos = {k: campos[k] for k in ("sala", "inicio", "fim", "responsavel")}
    tarefa.state = WORKING
    resultado = mcp.chamar_tool(TOOL_RESERVA, argumentos, trace_id=tarefa.trace_id)
    _concluir(tarefa, resultado, argumentos)


def _responder_pausa(tarefa: Task, texto: str) -> None:
    """Continuacao de uma Task pausada: `escolha=<id>` ou `escolha=recusar`."""
    pausa = tarefa.pausa
    assert pausa is not None
    escolha = _campos(texto).get("escolha", "").strip()

    if escolha == "recusar":
        tarefa.state = WORKING
        _retomar(tarefa, "decline", None)
        return

    if escolha not in pausa.alternativas:
        # Fora do enum: a Task continua pausada e a lista e repetida, igual.
        tarefa.dizer(f"alternativas: {', '.join(pausa.alternativas)}", INPUT_REQUIRED)
        return

    tarefa.state = WORKING
    _retomar(tarefa, "accept", escolha)


def get_task(params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id") or params.get("taskId") or ""
    tarefa = tarefas.obter(task_id)
    if tarefa is None:
        raise ErroA2A(-32001, f"Task nao encontrada: {task_id}")
    return {"task": tarefa.para_wire()}


# ------------------------------------------------------------------ agent card

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


# ------------------------------------------------------------------- transporte

METODOS = {"SendMessage", "GetTask"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "central-de-salas-a2a/1.0"

    def log_message(self, formato: str, *args: Any) -> None:  # noqa: N802
        log.info("%s %s", self.address_string(), formato % args)

    def _responder(self, status: int, corpo: dict[str, Any]) -> None:
        dados = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/.well-known/agent-card.json":
            self._responder(200, AGENT_CARD)
            return
        self._responder(404, {"error": "nao encontrado"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/a2a", ""):
            self._responder(404, {"error": "nao encontrado"})
            return

        tamanho = int(self.headers.get("Content-Length") or 0)
        try:
            pedido = json.loads(self.rfile.read(tamanho).decode() or "{}")
        except json.JSONDecodeError:
            self._responder(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return

        rpc_id = pedido.get("id")
        metodo = pedido.get("method")
        params = pedido.get("params") or {}

        cabecalho = self.headers.get("traceparent") or ""
        casado = RE_TRACEPARENT.match(cabecalho.strip())
        trace_id = casado.group(1) if casado else None

        if metodo not in METODOS:
            self._responder(
                200,
                {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Method not found: {metodo}"}},
            )
            return

        try:
            resultado = send_message(params, trace_id) if metodo == "SendMessage" else get_task(params)
        except ErroA2A as e:
            self._responder(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": e.code, "message": e.message}})
            return
        except Exception as e:  # noqa: BLE001
            log.exception("erro tratando %s", metodo)
            self._responder(
                200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32603, "message": f"Internal error: {e}"}}
            )
            return

        self._responder(200, {"jsonrpc": "2.0", "id": rpc_id, "result": resultado})


def main() -> None:
    servidor = ThreadingHTTPServer((HOST, PORTA), Handler)
    log.info("agente A2A em %s/a2a (card em %s/.well-known/agent-card.json)", URL_PUBLICA, URL_PUBLICA)
    log.info("servidor MCP em %s", URL_MCP)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        servidor.shutdown()


if __name__ == "__main__":
    main()
