# A Ponte: um agente A2A com MCP por dentro

Dois processos separados, falando um com o outro por HTTP de verdade:

- **`servidor-mcp/`** — servidor MCP em Streamable HTTP (revisão `2026-07-28`), com três tools, um resource e o ciclo completo de MRTR na reserva. Toda a regra de negócio de sala vive aqui.
- **`agente/`** — o agente: **host MCP por dentro** (descobre e chama as tools do servidor por HTTP) e **servidor A2A por fora** (Agent Card, `SendMessage`, `GetTask`). Não implementa nenhuma regra de sala: traduz protocolo.

Sem LLM em lugar nenhum. O pedido chega em formato fixo e é interpretado por string parsing, então o mesmo pedido sempre produz o mesmo resultado.

## Como rodar

A partir de um clone limpo. Python 3.10 ou superior (testado em 3.12).

### 1. Ambiente e dependências

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install mcp==2.2.0 uvicorn==0.53.0
```

O agente não tem dependência externa nenhuma: ele é construído sobre a biblioteca padrão (`urllib` para o cliente MCP, `http.server` para o endpoint A2A). O `pip install` acima serve ao servidor MCP.

### 2. Gerar e exportar o segredo do `requestState`

A chave de integridade vem sempre da variável de ambiente, nunca do código. Gere a sua:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

E exporte o valor gerado (os dois terminais abaixo precisam dele — na prática só o do servidor MCP o usa):

```bash
export REQUEST_STATE_SECRET=<o valor que você gerou>   # Windows: set REQUEST_STATE_SECRET=...
```

O servidor recusa subir se a variável estiver ausente ou tiver menos de 32 bytes. **Nunca comite o valor**: o repositório é público, e o `.gitignore` já ignora `.env`.

### 3. Terminal 1 — servidor MCP (porta 7301)

```bash
cd servidor-mcp
python3 servidor.py
```

Sobe em `http://127.0.0.1:7301/mcp`. Deixe o stderr visível: é nele que aparecem o método, o id e o `traceparent` de cada request.

### 4. Terminal 2 — agente (porta 7300)

```bash
cd agente
python3 agente.py
```

Sobe o endpoint JSON-RPC em `http://127.0.0.1:7300/a2a` e o card em `http://127.0.0.1:7300/.well-known/agent-card.json`.

### 5. Terminal 3 — validador

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Rode sempre com os dois processos recém-iniciados: as reservas criadas por uma execução mudam o resultado da seguinte.

### Variáveis de ambiente

| Variável | Padrão | Onde |
|---|---|---|
| `REQUEST_STATE_SECRET` | — (obrigatória) | servidor MCP |
| `MCP_PORT` | `7301` | servidor MCP |
| `MCP_HOST` | `127.0.0.1` | servidor MCP |
| `AGENTE_PORT` | `7300` | agente |
| `AGENTE_HOST` | `127.0.0.1` | agente |
| `AGENTE_URL` | `http://127.0.0.1:7300` | agente (url publicada no card) |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | agente (para onde ele fala MCP) |

## Onde a ponte acontece

A costura mora em **`agente/agente.py`**, em duas funções que se olham de frente.

**MCP → A2A, em `_pausar()` (`agente/agente.py:116`).** Quando o `tools/call` de `reservar_sala` volta com `resultType: "input_required"`, `_concluir()` (`agente/agente.py:163`, linha 165) desvia para `_pausar()`. Lá o agente lê a única entrada de `inputRequests`, extrai o `enum` (ou o `const`) da propriedade `sala` do `requestedSchema`, e faz três coisas: guarda a chave atribuída pelo servidor, guarda o `requestState` num objeto `Pausa` (`agente/tarefas.py:33`) **ligado àquela Task e só a ela**, e coloca a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids>`, na mesma ordem em que vieram no `enum`. O `requestState` fica no campo `Task.pausa` e nunca é serializado: `Task.para_wire()` (`agente/tarefas.py:86`) monta a Task para o cliente A2A sem tocar nele.

**A2A → MCP, em `_retomar()` (`agente/agente.py:140`).** O `SendMessage` de continuação traz `taskId` e `escolha=<valor>`. `_responder_pausa()` (`agente/agente.py:270`) valida a escolha contra o `enum` guardado — fora dele, a Task continua pausada e a lista é repetida igual — e chama `_retomar()`, que repete o **mesmo** `tools/call`, com os mesmos argumentos selados, levando `inputResponses` com **a mesma chave** que veio no `inputRequests` e o `requestState` **ecoado sem modificação**. O id de JSON-RPC novo sai de `ClienteMCP._proximo_id()` (`agente/cliente_mcp.py:48`), que incrementa um contador a cada chamada: nenhum id é reaproveitado, nunca, então o retry é sempre um request independente do original. `escolha=recusar` vira `action: "decline"` e a Task termina em `TASK_STATE_CANCELED`.

Do lado do servidor, o ponto correspondente é o resolver `escolha_de_sala()` (`servidor-mcp/servidor.py:141`). Ele roda **antes** do corpo da tool: sem conflito devolve `None` e a reserva segue direto; com conflito devolve um `Elicit`, e o SDK termina a resposta com `input_required` — o servidor nunca chama o cliente, ele encerra a resposta pedindo informação.

## Decisões técnicas

**Proteção do `requestState`.** Selado pelo próprio SDK, via `RequestStateSecurity(keys=[...])` (`servidor-mcp/servidor.py:117`): **AES-256-GCM** (AEAD — assina e cifra) sob uma chave derivada por **HKDF-SHA256** do segredo em `REQUEST_STATE_SECRET`. O envelope carrega `iat`/`exp`, o método, o alvo e um digest dos `arguments`, tudo sob a tag de autenticação. Trocar um caractere do token quebra a tag e a resposta é `-32602` com a mensagem fixa `Invalid or expired requestState` — o motivo real só vai para o log do servidor.

**TTL: 15 minutos** (`TTL_REQUEST_STATE = 900.0`), dentro da janela de 5 a 30 minutos que o enunciado pede.

**Argumentos adulterados no retry não tomam efeito.** O digest dos `arguments` faz parte do envelope autenticado, então um retry que troca a sala, o horário ou o responsável falha na verificação de vínculo e é recusado com `-32602`, antes de qualquer handler rodar. Dos dois caminhos que o enunciado aceita, este é o de rejeitar o estado.

**Restart não invalida o retry.** Como a chave vem da variável de ambiente e não é gerada por processo (`RequestStateSecurity.ephemeral()` seria exatamente o erro aqui), e como todo o estado viaja dentro do token e nada fica em memória entre o `input_required` e o retry, um retry apresentado depois de o servidor MCP ser reiniciado continua válido. Verificado à mão: o `requestState` foi obtido, o processo foi morto e subido de novo, e o mesmo token concluiu a operação.

**Onde mora o estado das Tasks.** Em memória, no processo do agente: um `dict` indexado pelo id da Task, em `DepositoDeTarefas` (`agente/tarefas.py`), protegido por um `threading.Lock` porque o servidor A2A é multithread. Cada Task carrega o seu próprio `Pausa` — é isso que faz duas Tasks pausadas ao mesmo tempo concluírem cada uma com a sua reserva, sem trocar de `requestState`. Não persiste em disco, e não precisa.

**Reservas.** Também em memória (`servidor-mcp/dominio.py`), carregadas de `dados/reservas.json` na subida. Reservas criadas durante a execução são visíveis para as consultas seguintes do mesmo processo e não sobrevivem a um restart, como o enunciado permite.

**O `requestState` é opaco para o agente.** Ele guarda e ecoa verbatim. Não abre, não decodifica, não interpreta — e nem poderia, já que o token é cifrado.

### Notas sobre o SDK

Duas observações sobre o comportamento do `mcp==2.2.0`, nenhuma delas contornada por reescrita de protocolo:

1. **Linhas extras de `tools/list` no log.** O transporte moderno valida os headers `Mcp-Param-*` de cada `tools/call` resolvendo o `inputSchema` da tool pelo próprio handler de `tools/list` (`mcp/server/_streamable_http_modern.py`, `_tool_input_schema`). Esse listing interno passa pelo middleware de log reusando o id do request de entrada, então aparece no stderr um `tools/list` sem `traceparent` logo antes de cada `tools/call`. É registro fiel de um request efetivamente despachado, e foi mantido em vez de filtrado. O `tools/list` do agente é identificável pelo id no formato `req-N-<hex>` e pelo `traceparent` presente.

2. **Uma resposta gravada vale para a pergunta exata que foi feita.** O SDK fixa cada resposta de elicitation ao digest da pergunta renderizada (`_request_digest` em `mcp/server/mcpserver/resolve.py`). Se, entre o `input_required` e o retry, o conjunto de alternativas mudar — porque outra Task reservou uma das salas oferecidas —, a pergunta é outra e a resposta gravada é descartada: o resolver pergunta de novo, ou a reserva falha com a mensagem de domínio. É a regra documentada de que o cálculo do resolver sempre vence o que o cliente ecoa de volta, e é o comportamento correto: uma corrida entre duas reservas do mesmo intervalo está fora do escopo do desafio.

## Saída do validador

Com os dois processos recém-iniciados, `echo $?` igual a `0`:

```
trace-id desta execucao: e32542deede67c7897d1d20ad4a9ab69
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
