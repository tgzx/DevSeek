import os
import json
from datetime import datetime
from pathlib import Path

DEVSEEK_DIR = ".devseek"
CONTEXT_FILE = "context.json"
INSTRUCTIONS_FILE = "instructions.md"
STRUCTURE_FILE = "structure.txt"

IGNORED = {"__pycache__", "node_modules", ".git", "venv", ".venv", "dist", "build", ".idea", ".vscode"}

# System block injected into every prompt — teaches DeepSeek the two-channel response format.
# Channel 1: [DEVSEEK_CHAT] — shown in the chat panel (explanation, reasoning, questions).
# Channel 2: [DEVSEEK_CREATE/UPDATE/...] — applied to project files (code only).
# This strict separation means NO code appears in the chat and NO explanation in files.
_DEVSEEK_CMD_BLOCK = """\
## Instruções do Sistema — DevSeek

Sua resposta usa dois canais separados. Siga o formato abaixo sem exceções.

━━━ CANAL 1 — chat com o usuário ━━━
[DEVSEEK_CHAT]
Escreva aqui APENAS texto para o usuário: explicação do que será feito, raciocínio,
perguntas, resumo. Use markdown normalmente (listas, negrito, etc.).

PROIBIDO neste bloco:
• Blocos de código (``` qualquer coisa ```)
• Nomes de marcadores do protocolo (não escreva [DEVSEEK_REPLACE], [DEVSEEK_FIM] etc.)
• Qualquer trecho de código — mesmo de exemplo
[/DEVSEEK_CHAT]

━━━ CANAL 2 — operações de arquivo ━━━

Escolha da operação — use EXATAMENTE a operação certa:

▸ REPLACE — mudança cirúrgica (use SEMPRE que possível para edições pontuais):
  Remover uma linha, corrigir um bug, renomear uma variável, ajustar um valor.
  O SEARCH deve conter o MÍNIMO de contexto suficiente para localizar o trecho.
  Inclua apenas as linhas que mudam + 1-2 linhas vizinhas para identificação única.
  Copie o SEARCH exatamente do contexto de arquivo fornecido, sem inventar linhas.
  Se você não tiver o trecho atual exato do arquivo, use UPDATE em vez de REPLACE.
[DEVSEEK_REPLACE: caminho/arquivo.ext]
SEARCH:
linha exata a ser encontrada no arquivo
REPLACE:
linha substituta (omita a linha inteira se quiser apagar)
[/DEVSEEK_REPLACE]

▸ UPDATE — substitui o arquivo inteiro (use APENAS quando mais de 40% do arquivo muda):
[DEVSEEK_UPDATE: caminho/arquivo.ext]
```linguagem
conteúdo completo novo
```
[/DEVSEEK_UPDATE]

▸ CREATE — cria arquivo novo (use APENAS para arquivos que ainda não existem):
[DEVSEEK_CREATE: caminho/arquivo.ext]
```linguagem
conteúdo completo
```
[/DEVSEEK_CREATE]

▸ Outras operações de uma linha:
[DEVSEEK_DELETE: caminho/arquivo.ext]
[DEVSEEK_MKDIR: caminho/pasta]
[DEVSEEK_MOVE: origem.ext -> destino.ext]
[DEVSEEK_RUN: comando aqui]

RUN no terminal:
- Use [DEVSEEK_RUN: ...] quando o usuario pedir para executar scripts, instalar dependencias,
  iniciar servidor, rodar testes, build, linter ou diagnostico.
- Quando voce emitir [DEVSEEK_RUN: ...], o DevSeek pode despachar esse comando
  para o terminal integrado do projeto.
- Nao diga que "nao tem acesso ao terminal" se [DEVSEEK_RUN] resolver a tarefa.
- Exemplos:
  [DEVSEEK_RUN: pnpm install]
  [DEVSEEK_RUN: pnpm dev]
  [DEVSEEK_RUN: pytest]

━━━ Múltiplos arquivos ━━━
Coloque [DEVSEEK_MAIS] entre cada bloco de arquivo quando houver mais de um:
[DEVSEEK_CREATE: a.js]
```javascript
...
```
[/DEVSEEK_CREATE]
[DEVSEEK_MAIS]
[DEVSEEK_CREATE: b.css]
```css
...
```
[/DEVSEEK_CREATE]
[DEVSEEK_FIM]

━━━ Regras obrigatórias ━━━
1. SEMPRE comece com [DEVSEEK_CHAT] — o chat vem antes de qualquer arquivo
2. NUNCA coloque código (```) dentro do [DEVSEEK_CHAT]
3. NUNCA escreva código fora de um bloco de operação de arquivo
4. Use [DEVSEEK_MAIS] entre múltiplos blocos de arquivo
5. Termine SEMPRE com [DEVSEEK_FIM] na última linha (nada depois)
6. Use caminhos relativos à raiz do projeto
7. Use a linguagem correta no bloco de código: python, html, css, js, json, etc.
8. Prefira REPLACE a UPDATE — não reescreva um arquivo inteiro para mudar 3 linhas
9. Se o usuario pedir para executar algo no projeto, prefira [DEVSEEK_RUN]
10. Nao invente a limitacao "nao tenho acesso ao terminal" quando [DEVSEEK_RUN] resolver a tarefa
"""

DEFAULT_INSTRUCTIONS = """# Instruções para o DeepSeek

## Regras Gerais
- Sempre explique o raciocínio antes de propor alterações de código
- Não faça deploy sem autorização explícita do usuário
- Não altere estrutura de banco de dados sem confirmação
- Não remova ou renomeie arquivos sem perguntar antes
- Apresente alterações de código como diffs quando possível

## Contexto do Projeto
Descreva aqui informações importantes sobre o projeto, tecnologias usadas,
padrões adotados e qualquer outra informação relevante para o assistente.

## Restrições
- Liste aqui classes ou módulos que não devem ser alterados sem autorização

## Fluxo npm do Projeto
(Quando solicitado pelo usuário, ou se for um projeto hospedado na vercel, feito com v0 por exemplo, você pode sugerir o seguinte fluxo de comandos npm para preparar o ambiente, auditar vulnerabilidades, corrigir problemas, para rodar o servidor local.)
- Para preparar o ambiente, use `npm install`.
- Para auditar vulnerabilidades, use `npm audit`.
- Para corrigir vulnerabilidades, use `npm audit fix`.
- Se necessário, use `npm audit fix --force` com cautela e teste o projeto depois.
- Para rodar o servidor local, use `npm run dev`.
- A porta padrão esperada é `http://localhost:3000`.
- Para outra porta, use `npm run dev -- -p 4000`.
"""

NPM_WORKFLOW_KNOWLEDGE = """## Fluxo npm do Projeto

### 1. Instalar dependências
- Necessário para preparar o ambiente e baixar todos os pacotes listados no package.json.
- No terminal, dentro da pasta do projeto, execute `npm install`.
- Aguarde a instalação dos pacotes e verifique se não houve erros.

### 2. Auditar vulnerabilidades
- Verifica se há falhas de segurança nas dependências instaladas.
- No terminal, execute `npm audit`.
- Analise os pacotes listados com problemas.
- Identifique se são dependências críticas ou apenas de desenvolvimento.

### 3. Corrigir vulnerabilidades
- Tenta corrigir automaticamente os problemas encontrados.
- No terminal, execute `npm audit fix`.
- Se ainda restarem falhas, use `npm audit fix --force`.
- Atenção: `--force` pode atualizar pacotes com breaking changes.
- Teste o projeto após aplicar as correções.

### 4. Rodar servidor local
- Inicia o servidor de desenvolvimento para acessar o projeto no navegador.
- No terminal, execute `npm run dev`.
- Aguarde a mensagem indicando que o servidor está rodando.
- Abra o navegador em `http://localhost:3000` (porta padrão).
- Se quiser outra porta, use `npm run dev -- -p 4000`.
"""


class ContextManager:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self.devseek_path = self.project_path / DEVSEEK_DIR

    def initialize(self):
        self.devseek_path.mkdir(exist_ok=True)

        context_path = self.devseek_path / CONTEXT_FILE
        if not context_path.exists():
            context = {
                "project_name": self.project_path.name,
                "created_at": datetime.now().isoformat(),
                "description": "",
                "tech_stack": [],
            }
            context_path.write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")

        instructions_path = self.devseek_path / INSTRUCTIONS_FILE
        if not instructions_path.exists():
            instructions_path.write_text(DEFAULT_INSTRUCTIONS, encoding="utf-8")

        self.update_structure()

    def update_structure(self):
        if not self.devseek_path.exists():
            return
        structure = self._generate_structure(self.project_path)
        (self.devseek_path / STRUCTURE_FILE).write_text(structure, encoding="utf-8")

    def _generate_structure(self, path: Path, prefix: str = "", depth: int = 0) -> str:
        if depth > 6:
            return ""

        lines = []
        if depth == 0:
            lines.append(f"[{path.name}]")

        try:
            items = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return ""

        visible = [i for i in items if i.name not in IGNORED and not i.name.startswith(".")]

        for idx, item in enumerate(visible):
            is_last = idx == len(visible) - 1
            connector = "└── " if is_last else "├── "
            icon = "📁 " if item.is_dir() else "📄 "

            lines.append(f"{prefix}{connector}{icon}{item.name}")

            if item.is_dir():
                extension = "    " if is_last else "│   "
                sub = self._generate_structure(item, prefix + extension, depth + 1)
                if sub:
                    lines.append(sub)

        return "\n".join(lines)

    def get_instructions(self) -> str:
        path = self.devseek_path / INSTRUCTIONS_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def get_structure(self) -> str:
        path = self.devseek_path / STRUCTURE_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def get_context(self) -> dict:
        path = self.devseek_path / CONTEXT_FILE
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def build_prompt(self, user_question: str, relevant_files: list, include_structure: bool = True, include_instructions: bool = True) -> str:
        parts = [_DEVSEEK_CMD_BLOCK]

        ctx = self.get_context()
        project_name = ctx.get("project_name", self.project_path.name)
        description = ctx.get("description", "")
        tech_stack = ctx.get("tech_stack", [])

        header = f"## Projeto: {project_name}"
        if description:
            header += f"\n{description}"
        if tech_stack:
            header += f"\nTecnologias: {', '.join(tech_stack)}"
        parts.append(header)

        if include_instructions:
            instructions = self.get_instructions()
            if instructions:
                parts.append(f"## Instruções e Regras\n{instructions}")

        if (self.project_path / "package.json").exists():
            parts.append(NPM_WORKFLOW_KNOWLEDGE)

        if include_structure:
            structure = self.get_structure()
            if structure:
                parts.append(f"## Estrutura do Projeto\n```\n{structure}\n```")

        if relevant_files:
            parts.append("## Arquivos Relevantes")
            for file_path, content in relevant_files:
                ext = Path(file_path).suffix.lstrip(".")
                lang = ext if ext else "text"
                parts.append(f"### {file_path}\n```{lang}\n{content}\n```")

        parts.append(f"## Pergunta\n{user_question}")

        return "\n\n---\n\n".join(parts)

    @property
    def is_initialized(self) -> bool:
        return self.devseek_path.exists()

    @property
    def instructions_path(self) -> Path:
        return self.devseek_path / INSTRUCTIONS_FILE

    @property
    def context_path(self) -> Path:
        return self.devseek_path / CONTEXT_FILE
