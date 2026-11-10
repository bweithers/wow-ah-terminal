"""Minimal, dependency-free parser for WoW SavedVariables (.lua) files.

SavedVariables are a restricted Lua subset: a sequence of ``NAME = <value>``
statements where values are numbers, strings, booleans, nil, or table
constructors. That is all this parses -- no function calls, no operators,
no code execution.
"""

from __future__ import annotations

import re

__all__ = ["parse", "parse_file", "LuaParseError"]


class LuaParseError(Exception):
    pass


_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<lcomment>--\[(?P<ceq>=*)\[.*?\](?P=ceq)\])
    | (?P<comment>--[^\n]*)
    | (?P<lstring>\[(?P<seq>=*)\[.*?\](?P=seq)\])
    | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
    | (?P<number>-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?))
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<punct>[{}\[\]=,;])
    """,
    re.X | re.S,
)

_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "\n": "\n",
}

_ESC_RE = re.compile(r"\\(z\s*|x[0-9a-fA-F]{2}|[0-9]{1,3}|.)", re.S)


def _unescape(raw: str) -> str:
    def sub(m: "re.Match[str]") -> str:
        body = m.group(1)
        if body.startswith("z"):
            return ""
        if body.startswith("x"):
            return chr(int(body[1:], 16))
        if body[0].isdigit():
            return chr(int(body))
        return _ESCAPES.get(body, body)

    return _ESC_RE.sub(sub, raw)


def _tokenize(text: str):
    pos, end = 0, len(text)
    tokens = []
    while pos < end:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            line = text.count("\n", 0, pos) + 1
            raise LuaParseError(f"unexpected character {text[pos]!r} on line {line}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comment", "lcomment", "ceq", "seq"):
            continue
        val = m.group()
        if kind == "string":
            tokens.append(("str", _unescape(val[1:-1])))
        elif kind == "lstring":
            open_end = val.index("[", 1) + 1
            close_start = val.rindex("]", 0, val.rindex("]"))
            tokens.append(("str", val[open_end:close_start].lstrip("\n")))
        elif kind == "number":
            low = val.lower()
            if low.startswith("0x") or low.startswith("-0x"):
                tokens.append(("num", int(val, 16)))
            elif any(c in val for c in ".eE"):
                tokens.append(("num", float(val)))
            else:
                tokens.append(("num", int(val)))
        elif kind == "name":
            low = val.lower()
            if low == "true":
                tokens.append(("bool", True))
            elif low == "false":
                tokens.append(("bool", False))
            elif low == "nil":
                tokens.append(("nil", None))
            else:
                tokens.append(("name", val))
        else:
            tokens.append((val, val))
    tokens.append(("eof", None))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.t = tokens
        self.i = 0

    def peek(self):
        return self.t[self.i]

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise LuaParseError(f"expected {kind!r}, got {tok[0]!r} ({tok[1]!r})")
        return tok

    def parse_chunk(self) -> dict:
        out = {}
        while self.peek()[0] != "eof":
            if self.peek()[0] in (",", ";"):
                self.next()
                continue
            name = self.expect("name")[1]
            self.expect("=")
            out[name] = self.parse_value()
        return out

    def parse_value(self):
        kind, val = self.next()
        if kind in ("str", "num", "bool", "nil"):
            return val
        if kind == "{":
            return self.parse_table()
        raise LuaParseError(f"unexpected token {kind!r} ({val!r}) in value position")

    def parse_table(self) -> dict:
        tbl = {}
        idx = 1
        while True:
            kind = self.peek()[0]
            if kind == "}":
                self.next()
                return tbl
            if kind in (",", ";"):
                self.next()
                continue
            if kind == "[":
                self.next()
                key = self.parse_value()
                self.expect("]")
                self.expect("=")
                tbl[key] = self.parse_value()
            elif kind == "name" and self.t[self.i + 1][0] == "=":
                key = self.next()[1]
                self.expect("=")
                tbl[key] = self.parse_value()
            else:
                tbl[idx] = self.parse_value()
                idx += 1


def parse(text: str) -> dict:
    """Parse SavedVariables source into {global_name: value}."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return _Parser(_tokenize(text)).parse_chunk()


def parse_file(path) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse(fh.read())
