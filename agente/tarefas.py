"""Task do A2A: identidade, estado, historico e produto.

Armazenamento em memoria, por processo, indexado pelo id da Task. O
`requestState` do MRTR mora aqui, ligado a Task que o recebeu, e nunca sai em
nenhuma resposta A2A: `para_wire()` simplesmente nao o serializa.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"

TERMINAIS = frozenset({COMPLETED, CANCELED, FAILED})

PAPEL_USUARIO = "ROLE_USER"
PAPEL_AGENTE = "ROLE_AGENT"


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O que o agente precisa guardar para retomar um MRTR interrompido."""

    chave: str
    """A chave que veio em inputRequests; o retry devolve exatamente ela."""
    request_state: str
    """Opaco. Guardado e ecoado, nunca aberto."""
    alternativas: list[str]
    argumentos: dict[str, Any]
    """Os argumentos do tools/call original, para repetir o request."""
    tool: str


@dataclass
class Task:
    id: str = field(default_factory=lambda: _id("task"))
    context_id: str = field(default_factory=lambda: _id("ctx"))
    state: str = SUBMITTED
    status_message: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pausa: Pausa | None = None
    trace_id: str | None = None

    # -------------------------------------------------------------- mutacoes

    def registrar_usuario(self, mensagem: dict[str, Any]) -> None:
        self.history.append(mensagem)

    def dizer(self, texto: str, estado: str) -> None:
        """Move a Task de estado e deixa a fala do agente no status e no historico."""
        mensagem = {
            "messageId": _id("msg"),
            "role": PAPEL_AGENTE,
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.state = estado
        self.status_message = mensagem
        self.history.append(mensagem)

    def anexar(self, nome: str, conteudo: str) -> None:
        self.artifacts.append(
            {"artifactId": _id("art"), "name": nome, "parts": [{"text": conteudo}]}
        )

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAIS

    # ------------------------------------------------------------------ wire

    def para_wire(self) -> dict[str, Any]:
        """A Task na forma que o cliente A2A ve.

        Nota deliberada: nem `pausa` nem `request_state` aparecem aqui.
        """
        status: dict[str, Any] = {"state": self.state}
        if self.status_message is not None:
            status["message"] = self.status_message
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(self.history),
            "artifacts": list(self.artifacts),
        }


class DepositoDeTarefas:
    """Tasks em memoria, por id. Nao persiste, e nao precisa."""

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._tarefas: dict[str, Task] = {}

    def nova(self) -> Task:
        tarefa = Task()
        with self._trava:
            self._tarefas[tarefa.id] = tarefa
        return tarefa

    def obter(self, task_id: str) -> Task | None:
        with self._trava:
            return self._tarefas.get(task_id)
