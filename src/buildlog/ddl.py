"""Small SQL DDL reader for PostgreSQL and Oracle table definitions.

It reads only what describes table structure: CREATE TABLE, COMMENT ON, ALTER TABLE ... ADD
constraints, and CREATE [UNIQUE] INDEX. Everything else is reported as a diagnostic with its
line span so a partial read is never presented as a complete one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SUPPORTED_DIALECTS = {"postgresql", "oracle"}

# Words that end a column's type and start its constraints.
_COLUMN_CLAUSE_WORDS = {
    "CONSTRAINT", "NOT", "NULL", "PRIMARY", "UNIQUE", "REFERENCES", "DEFAULT", "CHECK",
    "GENERATED", "COLLATE", "ENABLE", "DISABLE",
}
# Constraint-state words with no structural meaning for Buildlog.
_IGNORABLE_WORDS = {"ENABLE", "DISABLE", "VALIDATE", "NOVALIDATE", "DEFERRABLE", "INITIALLY", "DEFERRED", "IMMEDIATE", "RELY", "NORELY", "USING", "INDEX"}
_REFERENTIAL_ACTIONS = {"ON", "DELETE", "UPDATE", "CASCADE", "RESTRICT", "SET", "NULL", "DEFAULT", "NO", "ACTION", "MATCH", "FULL", "PARTIAL", "SIMPLE"}


@dataclass
class Token:
    kind: str  # ident, qident, string, number, punct, word
    value: str
    line: int

    @property
    def upper(self) -> str:
        return self.value.upper() if self.kind in {"ident", "word"} else self.value


class _Diag:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, severity: str, message: str, start: int, end: int, text: str = "") -> None:
        item: dict[str, Any] = {"severity": severity, "message": message, "start_line": start, "end_line": end}
        if text:
            item["text"] = text[:300]
        self.items.append(item)


def _strip_comments(sql: str) -> str:
    """Replace comments with spaces, keeping line numbers and string literals intact."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
        elif ch == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j == -1 else j
            out.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(re.sub(r"[^\n]", " ", sql[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[tuple[str, int, int]]:
    """Split on top-level ';' (and Oracle '/' lines). Returns (text, start_line, end_line)."""
    statements: list[tuple[str, int, int]] = []
    buf: list[str] = []
    line = 1
    start_line: int | None = None
    in_string = in_quote = False
    depth = 0
    lines = sql.split("\n")
    for line_index, raw_line in enumerate(lines):
        line = line_index + 1
        if not in_string and not in_quote and depth == 0 and raw_line.strip() == "/":
            if "".join(buf).strip():
                statements.append(("".join(buf).lstrip(), start_line or line, line))
            buf, start_line = [], None
            continue
        for ch in raw_line + ("\n" if line_index < len(lines) - 1 else ""):
            if start_line is None and not ch.isspace():
                start_line = line
            if ch == "'" and not in_quote:
                in_string = not in_string
            elif ch == '"' and not in_string:
                in_quote = not in_quote
            elif not in_string and not in_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth = max(0, depth - 1)
                elif ch == ";" and depth == 0:
                    if "".join(buf).strip():
                        statements.append(("".join(buf).lstrip(), start_line or line, line))
                    buf, start_line = [], None
                    continue
            buf.append(ch)
    if "".join(buf).strip():
        statements.append(("".join(buf).lstrip(), start_line or line, line))
    return statements


_TOKEN_RE = re.compile(
    r"""(?P<string>'(?:[^']|'')*')
      | (?P<qident>"[^"]*")
      | (?P<number>\d+(?:\.\d+)?)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_$#]*)
      | (?P<punct>[(),.])
      | (?P<other>\S)""",
    re.VERBOSE,
)


def _tokenize(text: str, first_line: int) -> list[Token]:
    tokens: list[Token] = []
    line = first_line
    position = 0
    for match in _TOKEN_RE.finditer(text):
        line += text.count("\n", position, match.start())
        position = match.start()
        kind = match.lastgroup or "other"
        value = match.group()
        if kind == "string":
            value = value[1:-1].replace("''", "'")
        elif kind == "qident":
            value = value[1:-1]
        tokens.append(Token("word" if kind == "other" else kind, value, line))
    return tokens


class _Cursor:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self, offset: int = 0) -> Token | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def at(self, *words: str) -> bool:
        for offset, word in enumerate(words):
            token = self.peek(offset)
            if token is None or token.kind not in {"ident", "word"} or token.upper != word:
                return False
        return True

    def take(self) -> Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def accept(self, *words: str) -> bool:
        if self.at(*words):
            self.i += len(words)
            return True
        return False

    def accept_punct(self, value: str) -> bool:
        token = self.peek()
        if token is not None and token.kind == "punct" and token.value == value:
            self.i += 1
            return True
        return False

    def done(self) -> bool:
        return self.i >= len(self.tokens)


def _ident(token: Token | None) -> str:
    if token is None or token.kind not in {"ident", "qident"}:
        raise _ParseError("expected an identifier")
    return token.value


class _ParseError(Exception):
    pass


def _qualified_name(cur: _Cursor) -> tuple[str | None, str]:
    first = _ident(cur.peek())
    cur.take()
    if cur.accept_punct("."):
        second = _ident(cur.peek())
        cur.take()
        return first, second
    return None, first


def _column_list(cur: _Cursor) -> list[str]:
    if not cur.accept_punct("("):
        raise _ParseError("expected '('")
    names: list[str] = []
    while True:
        names.append(_ident(cur.peek()))
        cur.take()
        # Oracle / PostgreSQL index columns may carry ASC/DESC.
        cur.accept("ASC") or cur.accept("DESC")
        if cur.accept_punct(","):
            continue
        if cur.accept_punct(")"):
            return names
        raise _ParseError("expected ',' or ')' in column list")


def _skip_balanced(cur: _Cursor) -> str:
    """Skip a parenthesised group starting at '(' and return its raw text."""
    if not cur.accept_punct("("):
        raise _ParseError("expected '('")
    depth = 1
    parts: list[str] = []
    while depth and not cur.done():
        token = cur.take()
        if token.kind == "punct" and token.value == "(":
            depth += 1
        elif token.kind == "punct" and token.value == ")":
            depth -= 1
            if depth == 0:
                break
        parts.append(token.value)
    return " ".join(parts)


def _split_top_level(tokens: list[Token]) -> list[list[Token]]:
    parts: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == "punct" and token.value == "(":
            depth += 1
        elif token.kind == "punct" and token.value == ")":
            depth -= 1
        if token.kind == "punct" and token.value == "," and depth == 0:
            parts.append([])
            continue
        parts[-1].append(token)
    return [part for part in parts if part]


def _references(cur: _Cursor) -> dict[str, Any]:
    target_schema, target_table = _qualified_name(cur)
    target_columns: list[str] = []
    token = cur.peek()
    if token is not None and token.kind == "punct" and token.value == "(":
        target_columns = _column_list(cur)
    while cur.peek() is not None and cur.peek().kind in {"ident", "word"} and cur.peek().upper in _REFERENTIAL_ACTIONS:  # type: ignore[union-attr]
        cur.take()
    return {"target_schema": target_schema, "target_table": target_table, "target_columns": target_columns}


def _table_constraint(cur: _Cursor, table: dict[str, Any]) -> None:
    name = None
    if cur.accept("CONSTRAINT"):
        name = _ident(cur.peek())
        cur.take()
    if cur.accept("PRIMARY", "KEY"):
        table["primary_key"] = _column_list(cur)
    elif cur.accept("UNIQUE"):
        table["unique"].append(_column_list(cur))
    elif cur.accept("FOREIGN", "KEY"):
        columns = _column_list(cur)
        if not cur.accept("REFERENCES"):
            raise _ParseError("expected REFERENCES")
        fk = {"columns": columns, **_references(cur)}
        if name:
            fk["name"] = name
        table["foreign_keys"].append(fk)
    elif cur.accept("CHECK"):
        table["checks"].append(_skip_balanced(cur))
    else:
        raise _ParseError("unknown table constraint")
    _skip_ignorable(cur)


def _skip_ignorable(cur: _Cursor) -> None:
    while not cur.done() and cur.peek().kind in {"ident", "word"} and cur.peek().upper in _IGNORABLE_WORDS:  # type: ignore[union-attr]
        cur.take()


def _column(tokens: list[Token], table: dict[str, Any], diag: _Diag, line: int) -> None:
    cur = _Cursor(tokens)
    name = _ident(cur.peek())
    cur.take()
    type_parts: list[str] = []
    while not cur.done():
        token = cur.peek()
        assert token is not None
        if token.kind in {"ident", "word"} and token.upper in _COLUMN_CLAUSE_WORDS:
            break
        if token.kind == "punct" and token.value == "(":
            type_parts.append("(" + _skip_balanced(cur).replace(" , ", ",") + ")")
            continue
        type_parts.append(cur.take().value)
    column: dict[str, Any] = {"name": name, "type": " ".join(type_parts).replace(" (", "("), "nullable": True}
    while not cur.done():
        if cur.accept("CONSTRAINT"):
            cur.take()
        elif cur.accept("NOT", "NULL"):
            column["nullable"] = False
        elif cur.accept("NULL"):
            column["nullable"] = True
        elif cur.accept("PRIMARY", "KEY"):
            table["primary_key"] = [name]
            column["nullable"] = False
        elif cur.accept("UNIQUE"):
            table["unique"].append([name])
        elif cur.accept("REFERENCES"):
            table["foreign_keys"].append({"columns": [name], **_references(cur)})
        elif cur.accept("CHECK"):
            table["checks"].append(_skip_balanced(cur))
        elif cur.accept("DEFAULT"):
            parts: list[str] = []
            while not cur.done():
                token = cur.peek()
                assert token is not None
                if token.kind in {"ident", "word"} and token.upper in _COLUMN_CLAUSE_WORDS - {"NULL"}:
                    break
                if token.kind == "punct" and token.value == "(":
                    parts.append("(" + _skip_balanced(cur) + ")")
                    continue
                parts.append(cur.take().value)
            column["default"] = " ".join(parts)
        elif cur.accept("COLLATE"):
            cur.take()
        elif cur.at("GENERATED"):
            parts = []
            while not cur.done() and not (cur.peek().kind in {"ident", "word"} and cur.peek().upper in {"NOT", "NULL", "PRIMARY", "UNIQUE", "REFERENCES", "CHECK", "CONSTRAINT"}):  # type: ignore[union-attr]
                token = cur.peek()
                if token is not None and token.kind == "punct" and token.value == "(":
                    parts.append("(" + _skip_balanced(cur) + ")")
                else:
                    parts.append(cur.take().value)
            column["generated"] = " ".join(parts)
        elif cur.peek().kind in {"ident", "word"} and cur.peek().upper in _IGNORABLE_WORDS:  # type: ignore[union-attr]
            cur.take()
        else:
            rest = " ".join(t.value for t in tokens[cur.i:])
            diag.add("unsupported", f"column {name}: clause not interpreted", line, line, rest)
            break
    table["columns"].append(column)


def _create_table(cur: _Cursor, text: str, start: int, end: int, diag: _Diag) -> dict[str, Any]:
    cur.accept("IF", "NOT", "EXISTS")
    schema, name = _qualified_name(cur)
    table: dict[str, Any] = {
        "schema": schema, "name": name, "description": None, "columns": [], "primary_key": [],
        "unique": [], "foreign_keys": [], "checks": [], "indexes": [], "span": {"start_line": start, "end_line": end},
    }
    if not cur.accept_punct("("):
        raise _ParseError("CREATE TABLE without a column list (e.g. CREATE TABLE AS) is not supported")
    depth = 1
    body: list[Token] = []
    while not cur.done():
        token = cur.take()
        if token.kind == "punct" and token.value == "(":
            depth += 1
        elif token.kind == "punct" and token.value == ")":
            depth -= 1
            if depth == 0:
                break
        body.append(token)
    if depth != 0:
        raise _ParseError("unbalanced parentheses")
    for element in _split_top_level(body):
        first = element[0]
        is_constraint = first.kind in {"ident", "word"} and first.upper in {"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}
        # A column can be called e.g. "unique" only when quoted; bare words above are constraints.
        if first.kind == "qident":
            is_constraint = False
        try:
            if is_constraint:
                element_cur = _Cursor(element)
                _table_constraint(element_cur, table)
                if not element_cur.done():
                    rest = " ".join(t.value for t in element[element_cur.i:])
                    diag.add("unsupported", f"table {name}: constraint clause not interpreted", first.line, element[-1].line, rest)
            else:
                _column(element, table, diag, first.line)
        except _ParseError as error:
            diag.add("unsupported", f"table {name}: {error}", first.line, element[-1].line, " ".join(t.value for t in element))
    if not cur.done():
        options = " ".join(t.value for t in cur.tokens[cur.i:])
        table["options_raw"] = options
        diag.add("info", f"table {name}: physical/table options kept as raw text, not interpreted", cur.peek().line, end, options)  # type: ignore[union-attr]
    return table


def parse_ddl(sql: str, dialect: str) -> dict[str, Any]:
    """Return {"tables": [...], "status": complete|partial|failed, "diagnostics": [...]}."""
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(f"unsupported DDL dialect: {dialect}")
    diag = _Diag()
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    comments: list[tuple[str, list[str], str, int, int]] = []
    alters: list[tuple[tuple[str | None, str], list[Token], int, int, str]] = []
    indexes: list[tuple[tuple[str | None, str], dict[str, Any], int, int]] = []

    for text, start, end in _split_statements(_strip_comments(sql)):
        tokens = _tokenize(text, start)
        if not tokens:
            continue
        cur = _Cursor(tokens)
        raw = " ".join(text.split())
        try:
            if cur.accept("CREATE"):
                cur.accept("OR", "REPLACE")
                for modifier in ("GLOBAL", "LOCAL", "TEMPORARY", "TEMP", "UNLOGGED", "PRIVATE"):
                    cur.accept(modifier)
                if cur.accept("TABLE"):
                    table = _create_table(cur, text, start, end, diag)
                    key = ((table["schema"] or "").casefold(), table["name"].casefold())
                    if key in tables:
                        diag.add("unsupported", f"table {table['name']} defined twice; later definition ignored", start, end)
                        continue
                    tables[key] = table
                    order.append(key)
                    continue
                unique = cur.accept("UNIQUE")
                if cur.accept("INDEX"):
                    cur.accept("IF", "NOT", "EXISTS")
                    index_name = _ident(cur.peek())
                    cur.take()
                    if not cur.accept("ON"):
                        raise _ParseError("expected ON")
                    target = _qualified_name(cur)
                    cur.accept("USING") and cur.take()
                    columns = _column_list(cur)
                    indexes.append((target, {"name": index_name, "columns": columns, "unique": unique}, start, end))
                    continue
                diag.add("unsupported", "statement not interpreted", start, end, raw)
            elif cur.accept("COMMENT", "ON"):
                if cur.accept("TABLE"):
                    schema, name = _qualified_name(cur)
                    parts = [p for p in (schema, name) if p]
                    kind = "table"
                elif cur.accept("COLUMN"):
                    parts = []
                    parts.append(_ident(cur.peek()))
                    cur.take()
                    while cur.accept_punct("."):
                        parts.append(_ident(cur.peek()))
                        cur.take()
                    kind = "column"
                else:
                    raise _ParseError("only COMMENT ON TABLE / COLUMN is supported")
                if not cur.accept("IS"):
                    raise _ParseError("expected IS")
                token = cur.take()
                if token.kind != "string":
                    raise _ParseError("expected a string literal")
                comments.append((kind, parts, token.value, start, end))
            elif cur.accept("ALTER", "TABLE"):
                cur.accept("ONLY")
                cur.accept("IF", "EXISTS")
                target = _qualified_name(cur)
                alters.append((target, tokens[cur.i:], start, end, raw))
            else:
                diag.add("unsupported", "statement not interpreted", start, end, raw)
        except _ParseError as error:
            diag.add("unsupported", str(error), start, end, raw)

    def find(schema: str | None, name: str, line: int, end_line: int) -> dict[str, Any] | None:
        if schema is not None:
            return tables.get((schema.casefold(), name.casefold()))
        matches = [tables[key] for key in order if key[1] == name.casefold()]
        if len(matches) > 1:
            diag.add("unsupported", f"table name {name} is ambiguous across schemas", line, end_line)
            return None
        return matches[0] if matches else None

    for target, rest, start, end, raw in alters:
        table = find(target[0], target[1], start, end)
        if table is None:
            diag.add("unsupported", f"ALTER TABLE for unknown table {target[1]}", start, end, raw)
            continue
        cur = _Cursor(rest)
        try:
            if not cur.accept("ADD"):
                raise _ParseError("only ALTER TABLE ... ADD constraint is supported")
            if cur.at("CONSTRAINT") or cur.at("PRIMARY") or cur.at("UNIQUE") or cur.at("FOREIGN") or cur.at("CHECK"):
                _table_constraint(cur, table)
            else:
                raise _ParseError("only ALTER TABLE ... ADD constraint is supported")
            if not cur.done():
                raise _ParseError("trailing clause not interpreted")
        except _ParseError as error:
            diag.add("unsupported", str(error), start, end, raw)

    for kind, parts, text, start, end in comments:
        if kind == "table":
            table = find(parts[0] if len(parts) == 2 else None, parts[-1], start, end)
            if table is None:
                diag.add("unsupported", f"comment for unknown table {'.'.join(parts)}", start, end)
                continue
            table["description"] = text
        else:
            if len(parts) < 2:
                diag.add("unsupported", "COMMENT ON COLUMN needs table.column", start, end)
                continue
            table = find(parts[-3] if len(parts) >= 3 else None, parts[-2], start, end)
            column = None if table is None else next((c for c in table["columns"] if c["name"].casefold() == parts[-1].casefold()), None)
            if column is None:
                diag.add("unsupported", f"comment for unknown column {'.'.join(parts)}", start, end)
                continue
            column["description"] = text

    for target, index, start, end in indexes:
        table = find(target[0], target[1], start, end)
        if table is None:
            diag.add("unsupported", f"index on unknown table {target[1]}", start, end)
            continue
        table["indexes"].append(index)
        if index["unique"]:
            table["unique"].append(index["columns"])

    # Foreign keys without target columns point at the target's primary key.
    for key in order:
        table = tables[key]
        for fk in table["foreign_keys"]:
            if fk["target_columns"]:
                continue
            target = find(fk["target_schema"], fk["target_table"], table["span"]["start_line"], table["span"]["end_line"])
            if target is not None and target["primary_key"]:
                fk["target_columns"] = list(target["primary_key"])
            else:
                diag.add("unsupported", f"table {table['name']}: foreign key target columns unknown", table["span"]["start_line"], table["span"]["end_line"])

    result_tables = [tables[key] for key in order]
    blocking = [d for d in diag.items if d["severity"] != "info"]
    if not result_tables:
        status = "failed"
    elif blocking:
        status = "partial"
    else:
        status = "complete"
    return {"tables": result_tables, "status": status, "diagnostics": diag.items}
