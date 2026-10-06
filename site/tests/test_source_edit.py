"""The test helper that edits copies of app source by structure: its edits land where they should."""

import ast

import source_edit as se


def _module(tmp_path, text: str):
    p = tmp_path / "src" / "lasto" / "m.py"
    p.parent.mkdir(parents=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_edits_land_on_the_right_line_after_characters_ast_doesnt_count_as_line_breaks(tmp_path):
    # A form feed (a PEP 8 section break) or U+2028 in a comment is a line break to str.splitlines, not to ast.
    p = _module(tmp_path, "X = 1\n\x0c\n# a comment with \u2028 in it\nY = 2\n")
    se.rebind(tmp_path, "m.py", "Y", lambda v: "3")
    assert p.read_text(encoding="utf-8") == "X = 1\n\x0c\n# a comment with \u2028 in it\nY = 3\n"


def test_a_keyword_is_added_to_a_call_however_it_is_wrapped(tmp_path):
    for layout in ("sub = f(name, help=x)\n", "sub = f(\n    name,\n    help=x,\n)\n", "sub = f(\n    name,\n    help=x\n    )\n"):
        p = tmp_path / "src" / "lasto" / "m.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(layout, encoding="utf-8")
        se.replace(tmp_path, "m.py", se.value("sub"), se.with_keyword("parents=[]"))
        call = ast.parse(p.read_text(encoding="utf-8")).body[0].value
        assert [k.arg for k in call.keywords] == ["help", "parents"], layout


def test_statements_go_in_at_the_found_statement_s_indentation(tmp_path):
    p = _module(tmp_path, "def f():\n    a = 1\n    b = 2\n")
    find = se.within("f", lambda n: isinstance(n, ast.Assign) and n.targets[0].id == "b", "b = 2")
    se.insert_before(tmp_path, "m.py", find, "if a:\n    pass")
    assert p.read_text(encoding="utf-8") == "def f():\n    a = 1\n    if a:\n        pass\n    b = 2\n"
