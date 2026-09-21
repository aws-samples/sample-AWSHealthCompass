"""Atlassian Document Format (ADF) builder for JIRA ticket descriptions.

Pure-Python functions that construct ADF JSON structures for JIRA Cloud
REST API v3. Every function is stateless, side-effect-free, and handles
None/empty inputs gracefully.

This module MUST NOT import boto3, urllib3, os, or any
I/O module. Only typing and stdlib data-structure modules.

All external values (tag values, ARNs, descriptions)
are placed inside {"type": "text", "text": value} nodes ONLY. They are
NEVER used as ADF node types, attrs keys, or marks type values.
Dependencies: Python stdlib only.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple, Union

# Type alias: a text item is either a plain string or (text, [marks]) tuple.
TextItem = Union[str, Tuple[str, List[str]]]


def _safe_str(value: Any) -> str:
    """Coerce value to non-None string."""
    if value is None:
        return ""
    return str(value)


def adf_doc(content: Sequence[dict]) -> dict:
    """Create an ADF document root node.

    Args:
        content: List of block-level ADF nodes.

    Returns:
        ADF document dict with version=1, type="doc".
    """
    return {
        "version": 1,
        "type": "doc",
        "content": list(content) if content else [],
    }


def adf_paragraph(texts: Sequence[TextItem]) -> dict:
    """Create a paragraph node from text items.

    Each item is either a plain string or a (text, marks) tuple where
    marks is a list of mark type strings (e.g. ["strong", "code"]).

    Args:
        texts: Sequence of text items.

    Returns:
        ADF paragraph node.
    """
    content = []
    for item in (texts or []):
        if item is None:
            content.append({"type": "text", "text": ""})
        elif isinstance(item, tuple):
            text_val, marks = item
            node = {"type": "text", "text": _safe_str(text_val)}
            if marks:
                node["marks"] = [{"type": m} for m in marks if m]
            content.append(node)
        else:
            content.append({"type": "text", "text": _safe_str(item)})
    return {"type": "paragraph", "content": content}


def adf_heading(text: Any, level: int = 3) -> dict:
    """Create a heading node.

    Args:
        text: Heading text.
        level: Heading level (1-6). Clamped to valid range.

    Returns:
        ADF heading node.
    """
    clamped = max(1, min(6, level))
    return {
        "type": "heading",
        "attrs": {"level": clamped},
        "content": [{"type": "text", "text": _safe_str(text)}],
    }


def adf_rule() -> dict:
    """Create a horizontal rule node."""
    return {"type": "rule"}


def adf_bold_value(label: str, value: Any) -> dict:
    """Create a paragraph with a bold label followed by a plain value.

    Example: **Campaign:** EKS 1.27 Deprecation

    Args:
        label: Bold label text (e.g. "Campaign: ").
        value: Plain value text.

    Returns:
        ADF paragraph node.
    """
    return adf_paragraph([
        (_safe_str(label), ["strong"]),
        _safe_str(value),
    ])


def adf_code(text: Any) -> dict:
    """Create an inline code text node (for use inside a paragraph).

    Returns a text node dict, NOT a paragraph. Caller must wrap in
    adf_paragraph() or place inside a table cell.

    Args:
        text: Code text (e.g. an ARN).

    Returns:
        ADF text node with code mark.
    """
    return {
        "type": "text",
        "text": _safe_str(text),
        "marks": [{"type": "code"}],
    }


def adf_bullet_list(items: Sequence[str]) -> dict:
    """Create a bullet list node from plain strings.

    ISEC-06: Accepts plain strings ONLY — not pre-built ADF nodes.
    Each string is wrapped in a code-marked text node inside
    listItem → paragraph (required by ADF spec).

    Args:
        items: Sequence of plain strings (e.g. resource ARNs).

    Returns:
        ADF bulletList node. Returns a paragraph with "None" if
        the input list is empty.
    """
    if not items:
        return adf_paragraph(["None"])

    list_items = []
    for item in items:
        text_node = {
            "type": "text",
            "text": _safe_str(item),
            "marks": [{"type": "code"}],
        }
        list_items.append({
            "type": "listItem",
            "content": [{"type": "paragraph", "content": [text_node]}],
        })
    return {"type": "bulletList", "content": list_items}


def adf_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict:
    """Create a table node with string headers and cell values.

    Each cell value is rendered as plain text. For code-formatted cells,
    use adf_table_rich() instead.

    Args:
        headers: Column header strings.
        rows: List of rows, each a list of cell values.

    Returns:
        ADF table node.
    """
    header_row = {
        "type": "tableRow",
        "content": [
            {
                "type": "tableHeader",
                "attrs": {},
                "content": [adf_paragraph([_safe_str(h)])],
            }
            for h in (headers or [])
        ],
    }
    data_rows = []
    for row in (rows or []):
        data_rows.append({
            "type": "tableRow",
            "content": [
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_paragraph([_safe_str(cell)])],
                }
                for cell in (row or [])
            ],
        })
    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": [header_row] + data_rows,
    }


def adf_table_rich(
    headers: Sequence[str],
    rows: Sequence[Sequence[Union[str, dict]]],
) -> dict:
    """Create a table with rich cell content.

    Each cell value is either a plain string or a pre-built ADF text
    node dict (e.g. from adf_code()). This allows mixing code-formatted
    ARNs with plain-text status values.

    Args:
        headers: Column header strings.
        rows: List of rows. Each cell is a string or ADF text node.

    Returns:
        ADF table node.
    """
    header_row = {
        "type": "tableRow",
        "content": [
            {
                "type": "tableHeader",
                "attrs": {},
                "content": [adf_paragraph([_safe_str(h)])],
            }
            for h in (headers or [])
        ],
    }
    data_rows = []
    for row in (rows or []):
        cells = []
        for cell in (row or []):
            if isinstance(cell, dict) and cell.get("type") == "text":
                # Pre-built ADF text node — wrap in paragraph
                para = {"type": "paragraph", "content": [cell]}
            else:
                para = adf_paragraph([_safe_str(cell)])
            cells.append({
                "type": "tableCell",
                "attrs": {},
                "content": [para],
            })
        data_rows.append({"type": "tableRow", "content": cells})
    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": [header_row] + data_rows,
    }
