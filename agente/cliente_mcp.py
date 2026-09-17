"""Cliente MCP cru do agente (host MCP).

Deliberadamente cru: nao usa a ClientSession do SDK com callback de elicitation,
porque um callback responderia a pergunta sozinho e a Task nunca pausaria. O
agente precisa enxergar o `resultType: input_required` como ele chega no fio.

Toda a construcao de `_meta` e dos headers espelhados vive aqui. Regra de
negocio de sala, nenhuma: isso e do servidor MCP.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("agente.mcp")

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}

# Metodos cujo corpo carrega um nome que o header Mcp-Name precisa espelhar.
CAMPO_DE_NOME = {"tools/call": "name", "resources/read": "uri"}


class ErroDeProtocolo(Exception):
    """O servidor MCP devolveu um `error` de JSON-RPC."""

    def __init__(self, erro: dict[str, Any]) -> None:
        super().__init__(erro.get("message", "erro de protocolo"))
        self.erro = erro
        self.code = erro.get("code")


class ClienteMCP:
    """Cliente Streamable HTTP sem sessao: cada request carrega o proprio `_meta`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._trava = threading.Lock()
        self._sequencia = 0

    def _proximo_id(self) -> str:
        """Id de JSON-RPC novo a cada chamada.

        E daqui que sai a garantia de que o retry do MRTR usa um id diferente do
        request original: nenhum id e reaproveitado, nunca.
        """
        with self._trava:
            self._sequencia += 1
            return f"req-{self._sequencia}-{secrets.token_hex(4)}"

    def chamar(self, metodo: str, params: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
        }
        if trace_id:
            # Mesmo trace-id do cliente A2A; span-id novo a cada salto.
            meta["traceparent"] = f"00-{trace_id}-{secrets.token_hex(8)}-01"

        corpo = {
            "jsonrpc": "2.0",
            "id": self._proximo_id(),
            "method": metodo,
            "params": {**params, "_meta": meta},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        campo = CAMPO_DE_NOME.get(metodo)
        if campo and params.get(campo) is not None:
            headers["Mcp-Name"] = str(params[campo])

        log.info("-> %s id=%s", metodo, corpo["id"])
        req = urllib.request.Request(self.url, data=json.dumps(corpo).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                bruto = r.read().decode()
        except urllib.error.HTTPError as e:
            bruto = e.read().decode()
        resposta = json.loads(bruto)
        if resposta.get("error"):
            raise ErroDeProtocolo(resposta["error"])
        return resposta.get("result") or {}

    # ------------------------------------------------------------------ atalhos

    def listar_tools(self, *, trace_id: str | None = None) -> list[dict[str, Any]]:
        return self.chamar("tools/list", {}, trace_id=trace_id).get("tools", [])

    def ler_resource(self, uri: str, *, trace_id: str | None = None) -> str:
        resultado = self.chamar("resources/read", {"uri": uri}, trace_id=trace_id)
        partes = resultado.get("contents") or []
        return "".join(p.get("text", "") for p in partes)

    def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            # Ecoado sem modificacao: o agente nunca abre nem interpreta este valor.
            params["requestState"] = request_state
        return self.chamar("tools/call", params, trace_id=trace_id)


def texto_do_resultado(resultado: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []) if p.get("type", "text") == "text")
