"""
CommandParser — extracts file operation commands from AI responses.

Primary format (plain text markers — reliable extraction from innerText):
  [DEVSEEK_CREATE: path]content[/DEVSEEK_CREATE]
  [DEVSEEK_UPDATE: path]content[/DEVSEEK_UPDATE]
  [DEVSEEK_REPLACE: path]SEARCH:\nold\nREPLACE:\nnew[/DEVSEEK_REPLACE]
  [DEVSEEK_DELETE: path]
  [DEVSEEK_MKDIR: path]
  [DEVSEEK_MOVE: src -> dst]

Legacy format (kept as fallback, inside ```devseek ... ``` blocks):
  create file <path> / update file <path> / delete file <path> / etc.
"""
import re
import json
import shutil
import difflib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ParsedCommand:
    action: str          # create_file | update_file | replace | delete | mkdir | move
    path: str            # primary path
    content: str = ""    # for create/update
    search: str = ""     # for replace
    replace: str = ""    # for replace
    dest: str = ""       # for move


@dataclass
class CommandResult:
    command: ParsedCommand
    success: Optional[bool]   # True=ok, False=error, None=preview (not applied yet)
    message: str
    diff: str = ""            # unified diff when applicable


# ── Plain-text marker patterns ────────────────────────────────────────────────

# Matches content with optional code fence wrapper.
# When the AI uses ```lang\n...\n``` inside the markers, the markdown renderer
# turns that into a <pre><code> block — textContent preserves indentation but
# strips the backticks.
# The language hint line may include residual UI labels that UI_NOISE didn't
# remove (e.g. "html Executar") — we allow spaces so the whole line is consumed.
_LANG_HINT = r'(?:[a-zA-Z][a-zA-Z0-9 +\-]*\n)?'

_PT_CREATE = re.compile(
    r'\[DEVSEEK_CREATE:\s*(.+?)\]\s*\n' + _LANG_HINT + r'(.*?)\n?\s*\[/DEVSEEK_CREATE\]',
    re.DOTALL)
_PT_UPDATE = re.compile(
    r'\[DEVSEEK_UPDATE:\s*(.+?)\]\s*\n' + _LANG_HINT + r'(.*?)\n?\s*\[/DEVSEEK_UPDATE\]',
    re.DOTALL)
_PT_REPLACE = re.compile(
    r'\[DEVSEEK_REPLACE:\s*(.+?)\]\s*\nSEARCH:\s*\n(.*?)(?:\n|\s+)REPLACE:\s*(?:\n)?(.*?)\n?\s*\[/DEVSEEK_REPLACE\]',
    re.DOTALL)
_PT_DELETE = re.compile(r'\[DEVSEEK_DELETE:\s*(.+?)\]')
_PT_MKDIR  = re.compile(r'\[DEVSEEK_MKDIR:\s*(.+?)\]')
_PT_MOVE   = re.compile(r'\[DEVSEEK_MOVE:\s*(.+?)\s*->\s*(.+?)\]')
_PT_RUN    = re.compile(r'\[DEVSEEK_RUN:\s*(.+?)\]')
_PT_CHAT   = re.compile(r'\[DEVSEEK_CHAT\](.*?)\[/DEVSEEK_CHAT\]', re.DOTALL)
# Matches any DEVSEEK marker that leaked into chat text (e.g. "[DEVSEEK_REPLACE]")
_PT_LEAKED_MARKER = re.compile(r'\[/?DEVSEEK_\w+[^\]]*\]')

# ── Legacy fenced-block patterns ──────────────────────────────────────────────

_DEVSEEK_BLOCK = re.compile(r'```devseek\s*\n(.*?)```', re.DOTALL)

_BLOCK_ACTIONS = re.compile(
    r'^(create file|update file|replace section|delete file|create dir|move file)\s+(\S+)',
    re.IGNORECASE,
)

# ── Pre-parse cleaners ───────────────────────────────────────────────────────

# Matches entire lines that are only a DeepSeek UI button label (case-insensitive).
# Must run BEFORE any regex sees the text so noise words don't corrupt content.
_UI_NOISE_LINE = re.compile(
    r'^\s*(Copiar|Baixar|Copy|Download|Expandir|Recolher|Executar|Run)\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# Matches a triple-backtick fence INSIDE a DEVSEEK_CREATE/UPDATE block.
# Handles both web-extracted text (no backticks, already stripped by browser) and
# raw API/clipboard text where the model wrote actual ```lang\ncontent\n``` fences.
# After normalization both formats look identical to _parse_plain_text.
#
# Input (API format):
#   [DEVSEEK_CREATE: file.py]\n```python\ndef f(): ...\n```\n[/DEVSEEK_CREATE]
# Output (normalised):
#   [DEVSEEK_CREATE: file.py]\npython\ndef f(): ...\n[/DEVSEEK_CREATE]
_FENCE_IN_BLOCK = re.compile(
    r'(\[DEVSEEK_(?:CREATE|UPDATE):[^\]]+\]\s*\n)'   # opening marker + newline
    r'```[ \t]*(\w*)\n'                               # ```lang
    r'(.*?)\n'                                        # code content
    r'```[ \t]*\n',                                   # closing ```
    re.DOTALL,
)


def _remove_ui_noise(text: str) -> str:
    text = _UI_NOISE_LINE.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', text)


def _normalize_devseek_blocks(text: str) -> str:
    """Strip ``` code fences from inside DEVSEEK_CREATE/UPDATE blocks.

    Leaves the language hint as a plain line so _LANG_HINT can skip it.
    No-op when backticks are already absent (normal web-extraction path).
    """
    def _replace(m):
        header  = m.group(1)   # [DEVSEEK_CREATE: ...]\n
        lang    = m.group(2)   # "python", "html", "" …
        content = m.group(3)   # actual code
        lang_line = (lang + '\n') if lang else ''
        return header + lang_line + content + '\n'

    return _FENCE_IN_BLOCK.sub(_replace, text)


def extract_chat_text(text: str) -> str | None:
    """Return the content of [DEVSEEK_CHAT]...[/DEVSEEK_CHAT] blocks joined together.

    Returns None when no such blocks exist — the caller should fall back to
    cleaning the full response text for display (backward-compatible behaviour).

    Also strips any DEVSEEK protocol markers that leaked into the chat text
    (e.g. when DeepSeek writes "[DEVSEEK_REPLACE]" in its explanation).
    """
    matches = _PT_CHAT.findall(text)
    if not matches:
        return None
    parts = []
    for m in matches:
        m = m.strip()
        if not m:
            continue
        m = _PT_LEAKED_MARKER.sub('', m)
        m = re.sub(r'\n{3,}', '\n\n', m).strip()
        if m:
            parts.append(m)
    return '\n\n'.join(parts) or None


def parse_commands(text: str) -> list[ParsedCommand]:
    """Extract all ParsedCommands from AI response text.

    Pipeline before any regex runs:
      1. _remove_ui_noise   — strip Copiar/Baixar/Executar lines
      2. _normalize_devseek_blocks — convert ```lang fences to plain lang line
    Both are no-ops when the text is already clean.

    Tries the plain-text marker format first (primary). Falls back to the
    legacy ```devseek``` fenced-block format if nothing is found.
    """
    text = _remove_ui_noise(text)
    text = _normalize_devseek_blocks(text)
    commands = _parse_plain_text(text)
    if not commands:
        # Legacy: ```devseek ... ``` blocks
        for block_m in _DEVSEEK_BLOCK.finditer(text):
            commands.extend(_parse_block(block_m.group(1)))
    return commands


def _parse_plain_text(text: str) -> list[ParsedCommand]:
    """Parse the plain-text [DEVSEEK_*] marker format.

    Commands are returned in document order (position in text), so MKDIR before
    CREATE when that's how the AI wrote them — important for the review dialog.
    """
    entries: list[tuple[int, ParsedCommand]] = []

    for m in _PT_CREATE.finditer(text):
        entries.append((m.start(), ParsedCommand(
            action="create_file", path=m.group(1).strip(),
            content=m.group(2).strip('\n'))))
    for m in _PT_UPDATE.finditer(text):
        entries.append((m.start(), ParsedCommand(
            action="update_file", path=m.group(1).strip(),
            content=m.group(2).strip('\n'))))
    for m in _PT_REPLACE.finditer(text):
        entries.append((m.start(), ParsedCommand(
            action="replace", path=m.group(1).strip(),
            search=m.group(2).strip('\n'), replace=m.group(3).strip('\n'))))
    for m in _PT_DELETE.finditer(text):
        path = m.group(1).strip()
        if not path.startswith('/'):
            entries.append((m.start(), ParsedCommand(action="delete", path=path)))
    for m in _PT_MKDIR.finditer(text):
        path = m.group(1).strip()
        if not path.startswith('/'):
            entries.append((m.start(), ParsedCommand(action="mkdir", path=path)))
    for m in _PT_MOVE.finditer(text):
        entries.append((m.start(), ParsedCommand(
            action="move", path=m.group(1).strip(), dest=m.group(2).strip())))
    for m in _PT_RUN.finditer(text):
        entries.append((m.start(), ParsedCommand(
            action="run", path=m.group(1).strip())))

    entries.sort(key=lambda x: x[0])
    return [cmd for _, cmd in entries]


def _parse_block(body: str) -> list[ParsedCommand]:
    commands: list[ParsedCommand] = []
    lines = body.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        m = _BLOCK_ACTIONS.match(line)
        if not m:
            i += 1
            continue

        action_raw = m.group(1).lower()
        path = m.group(2)

        if action_raw in ("create file", "update file"):
            action = "create_file" if action_raw == "create file" else "update_file"
            # Consume content between --- markers
            i += 1
            if i < len(lines) and lines[i].strip() == "---":
                i += 1
            content_lines = []
            while i < len(lines):
                if lines[i].strip() == "---":
                    i += 1
                    break
                content_lines.append(lines[i])
                i += 1
            commands.append(ParsedCommand(action=action, path=path,
                                          content="\n".join(content_lines)))
            continue

        if action_raw == "replace section":
            i += 1
            # Find <<<SEARCH / === / >>>REPLACE block
            search_lines, replace_lines = [], []
            in_search = False
            in_replace = False
            while i < len(lines):
                l = lines[i]
                if l.strip() == "<<<SEARCH":
                    in_search = True
                    i += 1
                    continue
                if l.strip() == "===":
                    in_search = False
                    in_replace = True
                    i += 1
                    continue
                if l.strip() == ">>>REPLACE":
                    i += 1
                    break
                if in_search:
                    search_lines.append(l)
                elif in_replace:
                    replace_lines.append(l)
                i += 1
            commands.append(ParsedCommand(
                action="replace",
                path=path,
                search="\n".join(search_lines),
                replace="\n".join(replace_lines),
            ))
            continue

        if action_raw == "delete file":
            commands.append(ParsedCommand(action="delete", path=path))
            i += 1
            continue

        if action_raw == "create dir":
            commands.append(ParsedCommand(action="mkdir", path=path))
            i += 1
            continue

        if action_raw == "move file":
            # "move file src to dst"
            rest = line[m.end():].strip()
            to_match = re.match(r'(.+?)\s+to\s+(.+)', rest + " " + " ".join(lines[i].split()[3:]))
            # simpler: the path is already captured, find "to <dst>" in remainder of line
            full_line = lines[i]
            mv = re.match(
                r'move file\s+(\S+)\s+to\s+(\S+)', full_line.strip(), re.IGNORECASE
            )
            if mv:
                commands.append(ParsedCommand(action="move", path=mv.group(1), dest=mv.group(2)))
            i += 1
            continue

        i += 1
    return commands


# ── Preview (compute diff without writing) ────────────────────────────────────

def preview_command(cmd: ParsedCommand, project_path: str) -> CommandResult:
    """Return a CommandResult with success=None and the diff computed but NO files written."""
    root = Path(project_path)
    target = (root / cmd.path).resolve()

    try:
        target.relative_to(root.resolve())
    except ValueError:
        return CommandResult(cmd, False, f"Caminho fora do projeto: {cmd.path}")

    if cmd.action == "create_file":
        content = _format_content(cmd.path, cmd.content)
        if target.exists():
            old = target.read_text(encoding="utf-8", errors="replace")
            diff = _make_diff(old, content, str(target))
            return CommandResult(cmd, None, f"Substituir: {cmd.path}", diff)
        return CommandResult(cmd, None, f"Criar novo: {cmd.path}", content)

    if cmd.action == "update_file":
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        content = _format_content(cmd.path, cmd.content)
        old = target.read_text(encoding="utf-8", errors="replace")
        return CommandResult(cmd, None, f"Atualizar: {cmd.path}",
                             _make_diff(old, content, str(target)))

    if cmd.action == "replace":
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        old = target.read_text(encoding="utf-8", errors="replace")
        new_content = _flexible_replace(old, cmd.search, cmd.replace)
        if new_content is None:
            return CommandResult(cmd, False, f"Trecho não encontrado em {cmd.path}")
        return CommandResult(cmd, None, f"Substituir trecho: {cmd.path}",
                             _make_diff(old, new_content, str(target)))

    if cmd.action == "delete":
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        old = target.read_text(encoding="utf-8", errors="replace")
        return CommandResult(cmd, None, f"Excluir: {cmd.path}",
                             _make_diff(old, "", str(target)))

    if cmd.action == "mkdir":
        msg = "Diretório já existe" if target.exists() else f"Criar diretório: {cmd.path}"
        return CommandResult(cmd, None, msg)

    if cmd.action == "move":
        dest = (root / cmd.dest).resolve()
        try:
            dest.relative_to(root.resolve())
        except ValueError:
            return CommandResult(cmd, False, f"Destino fora do projeto: {cmd.dest}")
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        return CommandResult(cmd, None, f"Mover: {cmd.path} → {cmd.dest}")

    if cmd.action == "run":
        # Execution is delegated to the terminal — preview just shows the command.
        return CommandResult(cmd, None, f"Executar no terminal: {cmd.path}")

    return CommandResult(cmd, False, f"Ação desconhecida: {cmd.action}")


# ── Applicator ────────────────────────────────────────────────────────────────

def apply_command(
    cmd: ParsedCommand,
    project_path: str,
    backup_dir: Optional[Path] = None,
) -> CommandResult:
    """Apply a single parsed command. Returns result with diff when available."""
    root = Path(project_path)
    target = (root / cmd.path).resolve()

    # Safety: keep within project
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return CommandResult(cmd, False, f"Caminho fora do projeto: {cmd.path}")

    if cmd.action == "create_file":
        content = _format_content(cmd.path, cmd.content)
        diff = ""
        if target.exists() and backup_dir:
            _backup(target, backup_dir)
            old = target.read_text(encoding="utf-8", errors="replace")
            diff = _make_diff(old, content, str(target))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return CommandResult(cmd, True, f"Criado: {cmd.path}", diff)
        except Exception as e:
            return CommandResult(cmd, False, f"Erro ao criar {cmd.path}: {e}")

    if cmd.action == "update_file":
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        content = _format_content(cmd.path, cmd.content)
        old = target.read_text(encoding="utf-8", errors="replace")
        diff = _make_diff(old, content, str(target))
        if backup_dir:
            _backup(target, backup_dir)
        try:
            target.write_text(content, encoding="utf-8")
            return CommandResult(cmd, True, f"Atualizado: {cmd.path}", diff)
        except Exception as e:
            return CommandResult(cmd, False, f"Erro ao atualizar {cmd.path}: {e}")

    if cmd.action == "replace":
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        old = target.read_text(encoding="utf-8", errors="replace")
        new_content = _flexible_replace(old, cmd.search, cmd.replace)
        if new_content is None:
            return CommandResult(cmd, False, f"Trecho não encontrado em {cmd.path}")
        diff = _make_diff(old, new_content, str(target))
        if backup_dir:
            _backup(target, backup_dir)
        try:
            target.write_text(new_content, encoding="utf-8")
            return CommandResult(cmd, True, f"Seção substituída: {cmd.path}", diff)
        except Exception as e:
            return CommandResult(cmd, False, f"Erro ao substituir em {cmd.path}: {e}")

    if cmd.action == "delete":
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        if backup_dir:
            _backup(target, backup_dir)
        try:
            target.unlink()
            return CommandResult(cmd, True, f"Excluído: {cmd.path}")
        except Exception as e:
            return CommandResult(cmd, False, f"Erro ao excluir {cmd.path}: {e}")

    if cmd.action == "mkdir":
        try:
            target.mkdir(parents=True, exist_ok=True)
            return CommandResult(cmd, True, f"Diretório criado: {cmd.path}")
        except Exception as e:
            return CommandResult(cmd, False, f"Erro ao criar diretório {cmd.path}: {e}")

    if cmd.action == "move":
        dest = (root / cmd.dest).resolve()
        try:
            dest.relative_to(root.resolve())
        except ValueError:
            return CommandResult(cmd, False, f"Destino fora do projeto: {cmd.dest}")
        if not target.exists():
            return CommandResult(cmd, False, f"Arquivo não encontrado: {cmd.path}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(dest))
            return CommandResult(cmd, True, f"Movido: {cmd.path} → {cmd.dest}")
        except Exception as e:
            return CommandResult(cmd, False, f"Erro ao mover {cmd.path}: {e}")

    if cmd.action == "run":
        # Signal to the caller that terminal execution is needed.
        # The actual dispatch happens in chat_panel._apply_commands().
        return CommandResult(cmd, None, f"Executar: {cmd.path}")

    return CommandResult(cmd, False, f"Ação desconhecida: {cmd.action}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _common_indent(lines: list) -> str:
    """Longest common leading whitespace among non-empty lines."""
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return ""
    prefix = non_empty[0][: len(non_empty[0]) - len(non_empty[0].lstrip())]
    for line in non_empty[1:]:
        ind = line[: len(line) - len(line.lstrip())]
        common = []
        for a, b in zip(prefix, ind):
            if a == b:
                common.append(a)
            else:
                break
        prefix = "".join(common)
        if not prefix:
            break
    return prefix


def _normalize_match_line(line: str) -> str:
    """Normalize a line for tolerant REPLACE matching.

    Existing projects often differ from AI snippets by BOM, tabs/spaces at the
    edges, or trailing whitespace. We intentionally ignore those differences
    while still requiring the core line content to match.
    """
    return line.rstrip('\r\n').replace('\ufeff', '').strip()


def _build_whitespace_flexible_pattern(search: str) -> str:
    """Build a regex that treats any whitespace run in SEARCH as flexible.

    This helps when the model copies the right HTML/JS tokens but collapses
    newlines/indentation into spaces, which is common in browser-extracted text.
    """
    tokens = re.split(r'(\s+)', search.strip().replace('\ufeff', ''))
    parts: list[str] = []
    for token in tokens:
        if not token:
            continue
        if token.isspace():
            parts.append(r'\s+')
        else:
            parts.append(re.escape(token))
    return r'(?:\ufeff)?' + ''.join(parts)


def _flexible_replace(old: str, search: str, replace: str) -> str | None:
    """Replace search with replace in old, with indentation-normalized fallback.

    Tries exact string match first. If that fails, matches each line after
    stripping leading whitespace, then re-applies the per-line indentation
    from the matched file block to each replacement line.

    Each replace line reuses the file indentation of the search line with the
    same stripped content — so varying indent levels within the block (e.g.
    a closing `}` at a shallower indent than the code above it) are preserved
    correctly. Falls back to the first window line's indentation for new lines
    that don't appear in the search block.
    """
    # Phase 1: exact match (fast path, preserves all whitespace exactly)
    if search in old:
        return old.replace(search, replace, 1)

    search_lines = search.splitlines()
    if not search_lines:
        return None
    search_norm = [_normalize_match_line(l) for l in search_lines]
    n = len(search_lines)

    old_lines = old.splitlines(keepends=True)

    for i in range(len(old_lines) - n + 1):
        window = old_lines[i:i + n]
        window_norm = [_normalize_match_line(l) for l in window]
        if window_norm != search_norm:
            continue

        # Build per-line lookup: stripped_content -> file indentation.
        # First occurrence wins when the same stripped content appears more than once.
        indent_map: dict = {}
        line_indents: list[str] = []
        for wl, norm in zip(window, search_norm):
            raw = wl.rstrip('\r\n')
            current_indent = raw[: len(raw) - len(raw.lstrip())]
            line_indents.append(current_indent)
            if norm not in indent_map:
                indent_map[norm] = current_indent

        # Fallback: indentation of the first window line (for new lines in replace).
        first_raw = window[0].rstrip('\r\n')
        fallback = first_raw[: len(first_raw) - len(first_raw.lstrip())]

        # Strip any common indent from the replace block, then re-apply per-line.
        replace_lines = replace.splitlines()
        rep_common = _common_indent(replace_lines)

        new_lines = []
        for idx, rl in enumerate(replace_lines):
            stripped = rl[len(rep_common):] if rl.startswith(rep_common) else rl.lstrip()
            position_indent = line_indents[idx] if idx < len(line_indents) else fallback
            indent = indent_map.get(_normalize_match_line(stripped), position_indent)
            new_lines.append(indent + stripped if stripped else stripped)

        last_win = window[-1]
        trail = '\r\n' if last_win.endswith('\r\n') else ('\n' if last_win.endswith('\n') else '')

        before = ''.join(old_lines[:i])
        after = ''.join(old_lines[i + n:])
        return before + '\n'.join(new_lines) + trail + after

    # Phase 3: whitespace-flexible regex match.
    # Useful when the model preserved the token sequence but collapsed line
    # breaks/spaces in SEARCH, which otherwise makes line-based matching fail.
    flexible_pattern = _build_whitespace_flexible_pattern(search)
    matches = list(re.finditer(flexible_pattern, old, re.DOTALL))
    if len(matches) == 1:
        match = matches[0]
        return old[:match.start()] + replace + old[match.end():]

    return None


def _format_content(path: str, content: str) -> str:
    """Normalize content before writing. With code-fence extraction indentation
    is already preserved, so we only fix JSON compactness here."""
    if Path(path).suffix.lower() == ".json":
        try:
            return json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except Exception:
            pass
    return content


def _make_diff(old: str, new: str, label: str) -> str:
    lines_a = old.splitlines(keepends=True)
    lines_b = new.splitlines(keepends=True)
    diff = difflib.unified_diff(lines_a, lines_b, fromfile=f"a/{label}", tofile=f"b/{label}", n=3)
    return "".join(diff)


def _backup(target: Path, backup_dir: Path):
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{ts}_{target.name}"
    try:
        shutil.copy2(str(target), str(backup_dir / backup_name))
    except Exception:
        pass
