"""Reusable, standard-library-only host extraction helpers for Nadgryzieni.

The pure parsers in this module never perform network access.  Fetching is kept
separate so callers can audit response failures without mistaking them for a
page that simply does not publish a ``Prowadzący`` block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen


class HostNameError(ValueError):
    """Raised when a source host name cannot be accepted safely."""


@dataclass
class HostParseResult:
    """Structured output shared by RRN and Patreon host parsers."""

    status: str
    hosts: List[str] = field(default_factory=list)
    excluded_hosts: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    source_url: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "hosts": list(self.hosts),
            "excluded_hosts": list(self.excluded_hosts),
            "diagnostics": list(self.diagnostics),
            "source_url": self.source_url,
            "provenance": dict(self.provenance),
        }


@dataclass
class ResponseValidationResult:
    """Safe response-integrity findings; response bodies are never included."""

    ok: bool
    diagnostics: List[str] = field(default_factory=list)
    status_code: Optional[int] = None
    content_type: str = ""
    final_url: str = ""


@dataclass
class FetchResult:
    """Bounded HTTPS fetch result with structured, body-free errors."""

    status: str
    body: str = ""
    source_url: str = ""
    final_url: str = ""
    status_code: Optional[int] = None
    content_type: str = ""
    diagnostics: List[str] = field(default_factory=list)


@dataclass
class _Node:
    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List[Any] = field(default_factory=list)
    parent: Optional["_Node"] = None


_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_ALLOWED_LIST_WRAPPERS = frozenset({"div", "section", "nav", "figure"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p"})
_CONTENT_MARKERS = frozenset(
    {
        "article-body",
        "article-content",
        "entry-content",
        "episode-content",
        "post-body",
        "post-content",
        "td-post-content",
    }
)
_SOCIAL_WORDS = frozenset(
    {
        "bluesky",
        "facebook",
        "github",
        "instagram",
        "linkedin",
        "mastodon",
        "patreon",
        "threads",
        "tiktok",
        "twitter",
        "youtube",
    }
)
_HOST_NAME_ALIASES = {
    "tomek pluszczyk": "Thomas Voland",
    "norbert cała": "NPC",
    "norbert": "NPC",
    "norbi": "NPC",
    "maciek buchert": "Maciej Buchert",
    "miłoszu": "Miłosz",
    "steve’a ballmera": "Steve Ballmer",
    "steve \"woz\" wozniak": "Steve \"Woz\" Wozniak",
    "steve 'woz' wozniak": "Steve \"Woz\" Wozniak",
    "steve “woz” wozniak": "Steve \"Woz\" Wozniak",
    "steve wozniak": "Steve \"Woz\" Wozniak",
    "michała „nozbe” śliwińskiego": "Michał Śliwiński",
}
_DESCRIPTION_PERSON_ALIASES = {
    "kamil": "Kamil Szmit",
    "zdzisiek": "Zdzisław Kaczyk",
    "zbyszek": "Zbigniew Sobiecki",
}
_DESCRIPTION_PERSON_MARKER_RE = re.compile(
    r"(?iu)(?<!\w)(?:gość|gościem|gości|goście|gośćmi|gościa|guest(?:s)?)(?!\w)"
)
_DESCRIPTION_PERSON_LABEL_RE = re.compile(
    r"(?iu)^(?:gosc|goscie|guest|guests)(?:\s+specjaln(?:y|i|e))?$"
)
_DESCRIPTION_NAME_TOKEN = (
    r"(?-i:(?:[A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż'’-]*|"
    r"[\"„][^\"”]{1,40}[\"”]))"
)
_DESCRIPTION_NAME_SEQUENCE = (
    rf"{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,2}}"
    rf"(?:\s*(?:,|i|oraz)\s*{_DESCRIPTION_NAME_TOKEN}"
    rf"(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,2}})*"
)
_DESCRIPTION_AFTER_IS_RE = re.compile(
    rf"(?iu)(?:gość|gościem|gości|goście|gośćmi|gościa|guest(?:s)?)"
    rf"[^.!?]{{0,180}}?\bjest\b\s+(?:to\s+)?"
    rf"(?P<names>{_DESCRIPTION_NAME_SEQUENCE})"
)
_DESCRIPTION_BEFORE_IS_RE = re.compile(
    rf"(?iu)(?P<name>{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,3}})"
    rf"\s+jest\s+(?:[^.!?]{{0,30}}?\s+)?gościem\b"
)
_DESCRIPTION_AFTER_DASH_RE = re.compile(
    rf"(?iu)(?:gość|gościem|gości|goście|gośćmi|gościa|guest(?:s)?)"
    rf"(?:(?![.!?]\s+).){{0,240}}?[–—-]\s*"
    rf"(?P<name>{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,3}})"
)
_DESCRIPTION_ROLE_RE = re.compile(
    rf"(?iu)(?:gość|gościem|gości|goście|gośćmi|gościa|guest(?:s)?)"
    rf"[^.!?]{{0,180}}?\b(?:pojawił(?:a)? się|robi|wystąpił(?:a)?|"
    rf"dołączył(?:a)?|zagrał(?:a)?)\b\s+"
    rf"(?P<name>{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,3}})"
)
_DESCRIPTION_CZYLI_RE = re.compile(
    rf"(?iu)(?:gość|gościem|gości|goście|gośćmi|gościa|guest(?:s)?)"
    rf"[^.!?]{{0,180}}?\b(?:czyli|to)\b\s+"
    rf"(?:(?:nasz|nasza|nasze|własny|własna|własne)\s+){{0,2}}"
    rf"(?P<name>{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,3}})"
)
_DESCRIPTION_NAME_AS_GUEST_RE = re.compile(
    rf"(?iu)(?P<name>{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,2}})"
    rf"(?:\s+(?:z|ze)\s+[^.!?]{{0,35}}?)?"
    rf"[^.!?]{{0,80}}?\b(?:specjalny|specjalna|special)\s+"
    rf"(?:gość|guest)\b"
)
_DESCRIPTION_AFTER_ARE_RE = re.compile(
    rf"(?iu)(?:gośćmi|goście|guests?)\s+(?:są|to)\s+"
    rf"(?P<names>{_DESCRIPTION_NAME_SEQUENCE})"
)
_DESCRIPTION_SPECIAL_COLON_RE = re.compile(
    rf"(?iu)(?:gość|guest)\s+specjaln(?:y|a|e)\s*:\s*"
    rf"(?P<name>{_DESCRIPTION_NAME_TOKEN}(?:\s+{_DESCRIPTION_NAME_TOKEN}){{0,3}})"
)
_DESCRIPTION_PREVIOUS_EPISODE_RE = re.compile(
    r"(?iu)\b(?:ostatni(?:ego|ej|im|m)?|poprzedni(?:ego|ej|im|m)?|last|previous)\s+"
    r"(?:odcinka|odcinku|episode)\b"
)
_DESCRIPTION_CONTEXT_WORDS = frozenset(
    {
        "a",
        "ale",
        "dzisiaj",
        "gości",
        "gościa",
        "goście",
        "gościem",
        "gość",
        "gośćmi",
        "guest",
        "guests",
        "nasz",
        "nasza",
        "nasze",
        "odcinku",
        "specjalna",
        "specjalne",
        "specjalny",
        "ten",
        "tym",
        "w",
        "własna",
        "własne",
        "własny",
    }
)
_DESCRIPTION_NON_PERSON_TOKENS = frozenset(
    {
        "amazon",
        "android",
        "aperture",
        "apple",
        "chatgpt",
        "chrome",
        "facebook",
        "firefox",
        "github",
        "google",
        "intel",
        "iphone",
        "ipad",
        "linux",
        "macbook",
        "microsoft",
        "mozilla",
        "nvidia",
        "openai",
        "patreon",
        "playstation",
        "silicon",
        "samsung",
        "sidebar",
        "sony",
        "spacex",
        "tesla",
        "twitter",
        "youtube",
    }
)
_DESCRIPTION_NON_PERSON_PHRASES = frozenset(
    {
        "open source",
        "retro rocket network",
    }
)


_SECRET_KEY_PATTERN = (
    r"authorization|api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"refresh[_-]?token|private[_-]?key|password|passwd|secret|token|"
    r"credential|credentials"
)


def _sanitize_public_text(value: str) -> str:
    """Redact credential-bearing URLs, remotes, headers, and values."""
    redacted = str(value)
    redacted = re.sub(
        r"(?i)(?:https?|ssh|git(?:\+ssh)?)\s*:(?:\\?/){2}[^\s<>\"']+",
        "[REDACTED_URL]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b[^/\s@]+@[^/\s:]+:[^\s]+",
        "[REDACTED_REMOTE]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)(\bauthorization\b(?:\s*[:=]\s*|\s+))[^\r\n]*",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)(\bpassword\s+for\b)[^\r\n]*",
        r"\1 [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r'''(?im)(\\*["']?\b(?:''' + _SECRET_KEY_PATTERN + r''')[A-Za-z0-9_-]*\\*["']?\s*[:=]\s*)(?:\\*"(?:\\.|[^"\\])*"\\*|\\*'(?:\\.|[^'\\])*'\\*|\\*\[[^\r\n\]]*\]\\*|[^\s,;}\]]+)''',
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        rf"(?im)(\b(?:{_SECRET_KEY_PATTERN})[A-Za-z0-9_-]*\s+)(?!for\b)[^\r\n]*",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r'''(?im)(\b(?:bearer|basic)\s+)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[A-Za-z0-9._~+/=-]+)''',
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?iu)\bnorbert\s+cała\b", "NPC", redacted)
    return redacted[:500]


def _safe_description_evidence(value: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", value).split()).strip()
    return _sanitize_public_text(value)


_SECRET_KEY_NAME_RE = re.compile(
    rf"(?i)(?:^|_)(?:{_SECRET_KEY_PATTERN})(?:_|$)"
)


def _is_secret_public_key(value: Any) -> bool:
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return bool(_SECRET_KEY_NAME_RE.search(text))


def _sanitize_public_key(value: Any) -> str:
    text = str(value)
    if _is_secret_public_key(text):
        return "[REDACTED_KEY]"
    return _safe_description_evidence(text)


def normalize_host_name(value: str) -> str:
    """NFKC-normalize, alias-normalize and whitespace-collapse one host name.

    The display spelling (including case and diacritics) is preserved after
    Unicode normalization, except for explicit reviewed aliases.  Table
    delimiters are rejected rather than rewritten, because silently changing
    them would corrupt a Markdown cell.
    """

    if not isinstance(value, str):
        raise HostNameError("host name must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise HostNameError("host name is empty")
    if ";" in normalized or "|" in normalized:
        raise HostNameError("host name contains a forbidden semicolon or pipe")
    return _HOST_NAME_ALIASES.get(normalized.casefold(), normalized)


def host_dedupe_key(value: str) -> str:
    """Return the canonical, case-insensitive key for one host name."""

    return normalize_host_name(value).casefold()


_RECORD_HOST_OVERRIDES = {
    "rk_b145e50a91b3e5e88ae300b4": {
        "hosts": ['Steve "Woz" Wozniak'],
        "hosts_status": "verified",
        "hosts_source": "manual",
        "hosts_source_url": "https://retrorocketnetwork.pl/nadgryzieni-71-woz",
        "provenance": {
            "kind": "manual_user_confirmation",
            "source_url": "https://retrorocketnetwork.pl/nadgryzieni-71-woz",
            "basis": "explicit_user_confirmation",
            "note": 'User confirmed that Steve "Woz" Wozniak appeared in this episode; the source page did not expose a structural Prowadzący block.',
        },
        "diagnostics": [
            "host added from explicit user confirmation; structural Prowadzący block was not available",
        ],
    },
}


_RECORD_HOST_EXCLUSIONS = {
    "rk_304f2d0f5a58d8938c47836e": {
        host_dedupe_key("Steve Ballmer"): {
            "action": "exclude",
            "name": "Steve Ballmer",
            "basis": "user_confirmed_not_host",
        },
    },
}


def apply_record_host_corrections(record_key: str, entry: dict) -> dict:
    """Apply reviewed aliases and record-scoped host exclusions idempotently."""

    corrected = dict(entry)
    raw_hosts = corrected.get("hosts")
    if not isinstance(raw_hosts, list):
        return corrected

    hosts = []
    seen = set()
    for raw_host in raw_hosts:
        if not isinstance(raw_host, str):
            hosts.append(raw_host)
            continue
        normalized = normalize_host_name(raw_host)
        key = host_dedupe_key(normalized)
        if key not in seen:
            seen.add(key)
            hosts.append(normalized)

    exclusions = _RECORD_HOST_EXCLUSIONS.get(str(record_key), {})
    applied = []
    if exclusions:
        retained = []
        for host in hosts:
            key = host_dedupe_key(host) if isinstance(host, str) else ""
            correction = exclusions.get(key)
            if correction is None:
                retained.append(host)
            elif correction not in applied:
                applied.append(dict(correction))
        hosts = retained

    corrected["hosts"] = hosts
    override = _RECORD_HOST_OVERRIDES.get(str(record_key))
    if override is not None:
        corrected.update({
            key: dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value
            for key, value in override.items()
        })
        return corrected

    if applied:
        if corrected.get("hosts_status") == "verified" and not hosts:
            corrected["hosts_status"] = "not_listed"
        provenance = corrected.get("provenance")
        if isinstance(provenance, dict):
            provenance = dict(provenance)
            existing = provenance.get("host_corrections")
            corrections = list(existing) if isinstance(existing, list) else []
            for correction in applied:
                if correction not in corrections:
                    corrections.append(correction)
            provenance["host_corrections"] = corrections
            corrected["provenance"] = provenance
    return corrected


def _normalize_description_person_name(value: str) -> str:
    normalized = normalize_host_name(value)
    return _DESCRIPTION_PERSON_ALIASES.get(normalized.casefold(), normalized)


def _node_text(node: _Node, include_nested_lists: bool = True) -> str:
    parts: List[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif include_nested_lists or child.tag not in {"ul", "ol"}:
            parts.append(_node_text(child, include_nested_lists=include_nested_lists))
    return " ".join(parts)


def _iter_nodes(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            for descendant in _iter_nodes(child):
                yield descendant


class _HTMLTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._stack: List[_Node] = [self.root]
        self.diagnostics: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(
            tag.lower(),
            {key.lower(): (value or "") for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(
            tag.lower(),
            {key.lower(): (value or "") for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not any(node.tag == tag for node in self._stack[1:]):
            self.diagnostics.append("unexpected closing tag")
            return
        if self._stack[-1].tag != tag:
            self.diagnostics.append("misnested HTML tags")
            while len(self._stack) > 1 and self._stack[-1].tag != tag:
                self._stack.pop()
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)

    def close(self) -> None:
        super().close()
        if len(self._stack) > 1:
            self.diagnostics.append("unclosed HTML element")


def _parse_html_tree(html: str) -> Tuple[_Node, List[str]]:
    if not isinstance(html, str):
        return _Node("#document"), ["HTML input must be text"]
    parser = _HTMLTreeParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return parser.root, ["HTML parser failed"]
    return parser.root, parser.diagnostics


def _class_tokens(node: _Node) -> List[str]:
    return [token.casefold() for token in node.attrs.get("class", "").split()]


def _is_content_container(node: _Node) -> bool:
    itemprop = node.attrs.get("itemprop", "").casefold()
    if itemprop == "articlebody":
        return True
    values = _class_tokens(node) + [node.attrs.get("id", "").casefold()]
    for value in values:
        if value in _CONTENT_MARKERS:
            return True
        if any(marker in value for marker in _CONTENT_MARKERS if marker != "td-post-content"):
            return True
    return False


def _contains(outer: _Node, inner: _Node) -> bool:
    return any(descendant is inner for descendant in _iter_nodes(outer))


def _content_roots(document: _Node) -> List[_Node]:
    nodes = list(_iter_nodes(document))
    marked = [node for node in nodes if _is_content_container(node)]
    if marked:
        return [node for node in marked if not any(_contains(node, other) for other in marked if other is not node)]
    articles = [node for node in nodes if node.tag == "article"]
    return [node for node in articles if not any(_contains(other, node) for other in articles if other is not node)]


def _normalise_marker(value: str, remove_diacritics: bool = False) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.split()).strip()
    if remove_diacritics:
        value = "".join(
            char
            for char in unicodedata.normalize("NFKD", value)
            if unicodedata.category(char) != "Mn"
        )
    return value.casefold()


def _is_hosts_heading(node: _Node) -> bool:
    if node.tag not in _HEADING_TAGS:
        return False
    text = " ".join(_node_text(node).split()).strip()
    text = text.rstrip(" :：\t")
    return _normalise_marker(text, remove_diacritics=True) == "prowadzacy"


def _has_list_ancestor(node: _Node) -> bool:
    current = node.parent
    while current is not None:
        if current.tag in {"ul", "ol", "li"}:
            return True
        current = current.parent
    return False


def _has_list_descendant(node: _Node) -> bool:
    return any(descendant.tag in {"ul", "ol", "li"} for descendant in _iter_nodes(node))


_NON_CONTENT_CLASS_MARKERS = frozenset({
    "aside",
    "banner",
    "blockquote",
    "citation",
    "footer",
    "header",
    "navigation",
    "nav",
    "pullquote",
    "quote",
    "related",
    "share",
    "sidebar",
    "side-bar",
    "social",
    "widget",
})
_NON_CONTENT_ROLES = frozenset({"banner", "complementary", "contentinfo", "navigation", "note"})


def _node_is_non_content(node: _Node) -> bool:
    if node.tag in {"aside", "blockquote", "caption", "figcaption", "footer", "header", "nav"}:
        return True
    role = node.attrs.get("role", "").casefold()
    if role in _NON_CONTENT_ROLES:
        return True
    values = _class_tokens(node) + [node.attrs.get("id", "").casefold()]
    for value in values:
        if value in _NON_CONTENT_CLASS_MARKERS or "sidebar" in value or "side-bar" in value:
            return True
        if any(
            value.startswith(prefix)
            for prefix in (
                "nav-",
                "nav_",
                "quote-",
                "quote_",
                "related-",
                "related_",
                "share-",
                "share_",
                "social-",
                "social_",
                "widget-",
                "widget_",
            )
        ):
            return True
    return False


def _has_non_content_context(node: _Node) -> bool:
    current = node
    while current is not None:
        if _node_is_non_content(current):
            return True
        current = current.parent
    return False


def _has_non_content_ancestor(node: _Node) -> bool:
    return _has_non_content_context(node.parent) if node.parent is not None else False


def _meaningful_children(node: _Node) -> List[Any]:
    return [
        child
        for child in node.children
        if not (isinstance(child, str) and not child.strip())
    ]


def _unwrap_list_wrapper(node: _Node) -> Optional[_Node]:
    if node.tag == "ul":
        return node
    if node.tag not in _ALLOWED_LIST_WRAPPERS:
        return None
    children = _meaningful_children(node)
    if len(children) != 1 or not isinstance(children[0], _Node):
        return None
    return _unwrap_list_wrapper(children[0])


def _following_unordered_list(heading: _Node) -> Tuple[Optional[_Node], str]:
    if heading.parent is None:
        return None, "host heading has no parent"
    siblings = heading.parent.children
    try:
        position = siblings.index(heading)
    except ValueError:
        return None, "host heading is not in its parent"
    following = [
        child
        for child in siblings[position + 1 :]
        if not (isinstance(child, str) and not child.strip())
    ]
    if not following:
        return None, "host heading has no following list"
    first = following[0]
    if not isinstance(first, _Node):
        return None, "host heading is followed by text, not a list"
    if _has_non_content_context(first):
        return None, "host heading is followed by a non-content list wrapper"
    list_node = _unwrap_list_wrapper(first)
    if list_node is None:
        return None, "host heading is not immediately followed by an unordered list"
    if _has_non_content_context(list_node):
        return None, "host list is inside non-content markup"
    return list_node, ""


def _valid_direct_list_items(list_node: _Node) -> Tuple[bool, List[_Node], str]:
    children = _meaningful_children(list_node)
    if not children:
        return True, [], ""
    if any(not isinstance(child, _Node) or child.tag != "li" for child in children):
        return False, [], "unordered list does not contain direct list items"
    # Older RRN templates wrap the real host list in one presentational <li>.
    # Unwrap only when that wrapper contains no text and exactly one nested list;
    # arbitrary nested/content lists remain fail-closed.
    if len(children) == 1:
        wrapper = children[0]
        wrapper_children = _meaningful_children(wrapper)
        if len(wrapper_children) == 1 and isinstance(wrapper_children[0], _Node) and wrapper_children[0].tag in {"ul", "ol"}:
            return _valid_direct_list_items(wrapper_children[0])
    if any(_has_list_descendant(child) for child in children):
        return False, [], "unordered list contains a nested list"
    if any(_has_non_content_context(child) for child in children):
        return False, [], "unordered list contains non-content markup"
    return True, [child for child in children if isinstance(child, _Node)], ""


def _is_social_link(node: _Node) -> bool:
    if node.tag != "a":
        return False
    text = _node_text(node).strip()
    haystack = " ".join(
        [
            node.attrs.get("class", ""),
            node.attrs.get("rel", ""),
            node.attrs.get("aria-label", ""),
            node.attrs.get("title", ""),
            node.attrs.get("href", ""),
            text,
        ]
    ).casefold()
    if any(word in haystack for word in _SOCIAL_WORDS):
        return True
    return bool(re.search(r"(?:^|[\s(\[])[@#][\w.-]+", text, flags=re.UNICODE))


def _text_before_social_link(node: _Node) -> str:
    parts: List[str] = []
    stopped = False

    def visit(current: _Node) -> None:
        nonlocal stopped
        if stopped or current.tag in {"ul", "ol"}:
            return
        if current.tag == "a" and _is_social_link(current):
            stopped = True
            return
        for child in current.children:
            if stopped:
                return
            if isinstance(child, str):
                parts.append(child)
            else:
                visit(child)

    visit(node)
    value = " ".join(parts)
    return _strip_handle_suffix(value)


def _strip_handle_suffix(value: str) -> str:
    value = " ".join(value.split()).strip()
    if not value:
        return value
    value = re.sub(
        r"\s*(?:[\[(]\s*)?@[\w][\w.:-]*(?:\s*[\])])?\s*$",
        "",
        value,
        flags=re.UNICODE,
    ).strip()
    value = re.sub(r"\s*[\[(][^()\[\]]*$", "", value).strip()
    return re.sub(r"\s*[:|,/;-]+$", "", value).strip()


def _node_is_struck(node: _Node) -> bool:
    for descendant in [node] + list(_iter_nodes(node)):
        if descendant.tag in {"del", "s", "strike"}:
            return True
        classes = set(_class_tokens(descendant))
        if classes.intersection({"strike", "strikethrough", "deleted"}):
            return True
    return False


def _normalise_entries(
    entries: Sequence[Tuple[str, bool]],
    source_url: str,
    diagnostics: Optional[List[str]] = None,
) -> HostParseResult:
    diagnostics = list(diagnostics or [])
    hosts: List[str] = []
    excluded: List[str] = []
    host_keys = set()
    excluded_keys = set()
    for raw_value, struck in entries:
        try:
            value = normalize_host_name(raw_value)
        except HostNameError as exc:
            diagnostics.append("host entry rejected: " + str(exc))
            return HostParseResult("parse_error", [], excluded, diagnostics, source_url)
        key = host_dedupe_key(value)
        if struck:
            if key not in excluded_keys:
                excluded.append(value)
                excluded_keys.add(key)
            continue
        if key not in host_keys:
            hosts.append(value)
            host_keys.add(key)
    status = "verified" if hosts else "not_listed"
    return HostParseResult(status, hosts, excluded, diagnostics, source_url)


def _description_marker_in_text(value: str) -> bool:
    return bool(_DESCRIPTION_PERSON_MARKER_RE.search(value))


def _description_label(node: _Node) -> bool:
    if node.tag not in _HEADING_TAGS or _has_list_ancestor(node) or _has_non_content_ancestor(node):
        return False
    value = _normalise_marker(_node_text(node), remove_diacritics=True)
    value = value.rstrip(" :：\t")
    return bool(_DESCRIPTION_PERSON_LABEL_RE.fullmatch(value))


def _description_sentence_names(value: str) -> List[str]:
    if _DESCRIPTION_PREVIOUS_EPISODE_RE.search(value):
        return []

    value = re.sub(
        r"(?iu)\b(?:dr|prof(?:esor)?|mgr|inż|pan|pani|mr|mrs|ms)\.?\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])",
        "",
        value,
    )

    def candidate_allowed(candidate: str) -> bool:
        tokens = candidate.strip().split()
        if not tokens:
            return False
        normalized_tokens = [token.strip("\"„”'’,-:;.!?").casefold() for token in tokens]
        if normalized_tokens[0] in _DESCRIPTION_CONTEXT_WORDS:
            return False
        if any(
            token in {"gość", "gościem", "goście", "gości", "gościa", "gośćmi", "guest", "guests"}
            for token in normalized_tokens
        ):
            return False
        if any(token in _DESCRIPTION_NON_PERSON_TOKENS for token in normalized_tokens):
            return False
        if " ".join(normalized_tokens) in _DESCRIPTION_NON_PERSON_PHRASES:
            return False
        if candidate.lstrip().startswith(("\"", "„")):
            return False
        return True

    def split_sequence(value_to_split: str) -> List[str]:
        return [
            piece.strip(" ,;:.!?–—-\t")
            for piece in re.split(r"\s*(?:,|\bi\b|\boraz\b)\s*", value_to_split, flags=re.I)
            if piece.strip(" ,;:.!?–—-\t")
        ]

    names: List[str] = []
    patterns = (
        _DESCRIPTION_AFTER_ARE_RE,
        _DESCRIPTION_SPECIAL_COLON_RE,
        _DESCRIPTION_AFTER_IS_RE,
        _DESCRIPTION_BEFORE_IS_RE,
        _DESCRIPTION_AFTER_DASH_RE,
        _DESCRIPTION_ROLE_RE,
        _DESCRIPTION_CZYLI_RE,
        _DESCRIPTION_NAME_AS_GUEST_RE,
    )
    for pattern in patterns:
        for match in pattern.finditer(value):
            group_name = "names" if "names" in match.groupdict() else "name"
            candidates = (
                split_sequence(match.group(group_name))
                if group_name == "names"
                else [match.group(group_name)]
            )
            for candidate in candidates:
                candidate = candidate.strip(" ,;:.!?–—-\t")
                candidate_tokens = candidate.split()
                while candidate_tokens and candidate_tokens[0].strip("\"„”'’,-:;.!?").casefold() in _DESCRIPTION_CONTEXT_WORDS:
                    candidate_tokens.pop(0)
                candidate = " ".join(candidate_tokens)
                if candidate and candidate_allowed(candidate) and candidate not in names:
                    names.append(candidate)
    return names


def _normalise_description_entries(
    entries: Sequence[Tuple[str, bool]],
    source_url: str,
    diagnostics: Optional[List[str]] = None,
) -> Tuple[List[str], List[str], List[str], List[Dict[str, str]]]:
    diagnostics = list(diagnostics or [])
    people: List[str] = []
    excluded: List[str] = []
    corrections: List[Dict[str, str]] = []
    correction_keys = set()
    people_keys = set()
    excluded_keys = set()
    for raw_value, struck in entries:
        try:
            source_name = " ".join(unicodedata.normalize("NFKC", raw_value).split())
            alias_normalized = normalize_host_name(source_name)
            value = _DESCRIPTION_PERSON_ALIASES.get(alias_normalized.casefold(), alias_normalized)
        except HostNameError as exc:
            diagnostics.append("description person entry rejected: " + str(exc))
            return [], excluded, diagnostics, corrections
        if source_name.casefold() != alias_normalized.casefold():
            correction = {
                "source_name": source_name,
                "canonical_name": alias_normalized,
                "basis": "reviewed_host_alias",
            }
            correction_key = tuple(correction.values())
            if correction_key not in correction_keys:
                corrections.append(correction)
                correction_keys.add(correction_key)
        if alias_normalized.casefold() != value.casefold():
            correction = {
                "source_name": alias_normalized,
                "canonical_name": value,
                "basis": "user_confirmed_identity",
            }
            correction_key = tuple(correction.values())
            if correction_key not in correction_keys:
                corrections.append(correction)
                correction_keys.add(correction_key)
        key = host_dedupe_key(value)
        if struck:
            if key not in excluded_keys:
                excluded.append(value)
                excluded_keys.add(key)
            continue
        if key not in people_keys:
            people.append(value)
            people_keys.add(key)
    return people, excluded, diagnostics, corrections


def _extract_rrn_description_people(
    document: _Node,
    roots: Sequence[_Node],
    source_url: str,
) -> Tuple[List[str], List[str], List[str], Dict[str, Any]]:
    """Extract explicit description people without creating a second public type."""
    entries: List[Tuple[str, bool]] = []
    diagnostics: List[str] = []
    evidence: List[Dict[str, str]] = []
    structural_blocks = 0
    direct_markers = 0
    unsafe_marker = False

    for root in roots:
        for node in _iter_nodes(root):
            if not _description_label(node):
                continue
            structural_blocks += 1
            if _has_non_content_context(node):
                diagnostics.append("description people label is inside non-content context")
                continue
            marker = " ".join(_node_text(node).split()).strip().rstrip(" :：\t")
            list_node, reason = _following_unordered_list(node)
            if list_node is None:
                diagnostics.append("description people label has no valid following list: " + reason)
                continue
            if _has_non_content_context(list_node):
                diagnostics.append("description people list is inside non-content context")
                continue
            valid, items, list_error = _valid_direct_list_items(list_node)
            if not valid:
                diagnostics.append("description people list is malformed: " + list_error)
                continue
            block_evidence = []
            for item in items:
                if _has_non_content_context(item):
                    diagnostics.append("description people list item is inside non-content context")
                    continue
                value = _text_before_social_link(item)
                if not value:
                    diagnostics.append("description people list contains an empty entry")
                    continue
                entries.append((value, _node_is_struck(item)))
                block_evidence.append(_safe_description_evidence(value))
            evidence.append({"marker": _safe_description_evidence(marker), "entries": " | ".join(block_evidence)})

        for node in _iter_nodes(root):
            if (
                node.tag != "p"
                or _has_list_ancestor(node)
                or _has_list_descendant(node)
                or _has_non_content_context(node)
                or _description_label(node)
            ):
                continue
            text = " ".join(_node_text(node).split()).strip()
            if not _description_marker_in_text(text):
                continue
            direct_markers += 1
            if "|" in text:
                unsafe_marker = True
                diagnostics.append("description people evidence contains a table delimiter")
                continue
            names = _description_sentence_names(text)
            if names:
                entries.extend((name, False) for name in names)
                evidence.append({"marker": "prose", "evidence": _safe_description_evidence(text)})
            else:
                diagnostics.append(
                    "description people marker has no unambiguous name: "
                    + _safe_description_evidence(text)[:300]
                )

    people, excluded, diagnostics, identity_corrections = _normalise_description_entries(
        entries, source_url, diagnostics
    )
    if structural_blocks > 1:
        diagnostics.append("multiple description people blocks found; refusing ambiguous extraction")
        people = []
        identity_corrections = []
    if unsafe_marker:
        people = []
        identity_corrections = []
    if not people and not structural_blocks and not direct_markers:
        all_content = " ".join(_node_text(root) for root in roots)
        if _description_marker_in_text(all_content):
            diagnostics.append("description people marker appears only in nested/list content")
    provenance: Dict[str, Any] = {}
    if evidence:
        provenance = {
            "kind": "direct_source",
            "source_url": source_url,
            "description_evidence": evidence,
        }
        if identity_corrections:
            provenance["description_identity_corrections"] = identity_corrections
    return people, excluded, diagnostics, provenance


def _merge_description_people(
    result: HostParseResult,
    document: _Node,
    roots: Sequence[_Node],
) -> HostParseResult:
    people, excluded, diagnostics, provenance = _extract_rrn_description_people(
        document, roots, result.source_url
    )
    result.diagnostics.extend(diagnostics)
    existing_keys = {host_dedupe_key(host) for host in result.hosts}
    for person in people:
        key = host_dedupe_key(person)
        if key not in existing_keys:
            result.hosts.append(person)
            existing_keys.add(key)
    excluded_keys = {host_dedupe_key(host) for host in result.excluded_hosts}
    for person in excluded:
        key = host_dedupe_key(person)
        if key not in excluded_keys:
            result.excluded_hosts.append(person)
            excluded_keys.add(key)
    if people and result.status == "not_listed":
        result.status = "verified"
    if provenance:
        result.provenance = provenance
    return result


def _source_url_or_error(source_url: str) -> Tuple[str, Optional[str]]:
    if not source_url:
        return "", None
    try:
        parsed = urlsplit(source_url.strip())
        if parsed.fragment:
            source_url = urlunsplit(parsed._replace(fragment=""))
        return canonical_url(source_url), None
    except ValueError as exc:
        return "", "invalid source URL: " + str(exc)


def _marker_diagnostics(document: _Node, expected_title: str, expected_episode: str) -> List[str]:
    if not expected_title and not expected_episode:
        return []
    text = _normalise_marker(_node_text(document))
    diagnostics: List[str] = []
    if expected_title and _normalise_marker(expected_title) not in text and not _title_body_marker_present(expected_title, text):
        diagnostics.append("expected title marker not found")
    if expected_episode and _normalise_marker(expected_episode) not in text and not _title_body_marker_present(expected_title, text):
        diagnostics.append("expected episode marker not found")
    return diagnostics


def _title_body_marker_present(expected_title: str, normalized_text: str) -> bool:
    """Accept legacy pages whose title omits the archive's fractional ID."""
    if not expected_title:
        return False
    candidates = [expected_title]
    if ":" in expected_title:
        body = expected_title.split(":", 1)[1].strip()
        body = re.sub(r"^\([^)]*\)\s*", "", body)
        candidates.append(body)
    return any(
        len(_normalise_marker(candidate)) >= 8 and _normalise_marker(candidate) in normalized_text
        for candidate in candidates
    )


def parse_rrn_hosts(
    html: str,
    expected_url: str = "",
    expected_title: str = "",
    expected_episode: str = "",
) -> HostParseResult:
    """Extract the one structural ``Prowadzący`` list from an RRN page."""

    source_url, url_error = _source_url_or_error(expected_url)
    if url_error:
        return HostParseResult("parse_error", diagnostics=[url_error])
    document, parser_diagnostics = _parse_html_tree(html)
    fatal_parser_diagnostics = {
        "HTML input must be text",
        "HTML parser failed",
        "misnested HTML tags",
        "unclosed HTML element",
    }
    if any(diagnostic in fatal_parser_diagnostics for diagnostic in parser_diagnostics):
        return HostParseResult("parse_error", diagnostics=list(parser_diagnostics), source_url=source_url)
    marker_errors = _marker_diagnostics(document, expected_title, expected_episode)
    if marker_errors:
        return HostParseResult("parse_error", diagnostics=marker_errors, source_url=source_url)

    roots = _content_roots(document)
    candidates: List[Tuple[_Node, Optional[_Node], str]] = []
    for root in roots:
        for node in _iter_nodes(root):
            if _is_hosts_heading(node) and not _has_list_ancestor(node) and not _has_non_content_ancestor(node):
                list_node, reason = _following_unordered_list(node)
                candidates.append((node, list_node, reason))

    if not candidates:
        result = HostParseResult(
            "not_listed",
            diagnostics=["no structural Prowadzący block found"],
            source_url=source_url,
        )
        return _merge_description_people(result, document, roots)
    if len(candidates) > 1:
        return HostParseResult(
            "parse_error",
            diagnostics=["multiple ambiguous Prowadzący blocks found"],
            source_url=source_url,
        )
    _, list_node, reason = candidates[0]
    if list_node is None:
        return HostParseResult("parse_error", diagnostics=[reason], source_url=source_url)
    valid, list_items, list_error = _valid_direct_list_items(list_node)
    if not valid:
        return HostParseResult("parse_error", diagnostics=[list_error], source_url=source_url)

    entries: List[Tuple[str, bool]] = []
    for item in list_items:
        value = _text_before_social_link(item)
        if not value:
            return HostParseResult(
                "parse_error",
                diagnostics=["host list contains an empty entry"],
                source_url=source_url,
            )
        entries.append((value, _node_is_struck(item)))
    return _merge_description_people(
        _normalise_entries(entries, source_url),
        document,
        roots,
    )


def _payload_content_json(payload: Any) -> Tuple[Optional[Any], Optional[str]]:
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None, "Patreon payload is not valid JSON"
    if not isinstance(payload, Mapping):
        return None, "Patreon content_json_string unavailable"
    stack: List[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if "content_json_string" in current:
                value = current["content_json_string"]
                if value is None or value == "":
                    return None, "Patreon content_json_string unavailable"
                return value, None
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return None, "Patreon content_json_string unavailable"


def _pm_node_text(node: Mapping[str, Any], include_nested_lists: bool = True) -> str:
    node_type = str(node.get("type", ""))
    if node_type == "text":
        return str(node.get("text", ""))
    if not include_nested_lists and node_type in {"bulletList", "orderedList"}:
        return ""
    content = node.get("content", [])
    if not isinstance(content, list):
        return ""
    return " ".join(
        _pm_node_text(child, include_nested_lists=include_nested_lists)
        for child in content
        if isinstance(child, Mapping)
    )


def _pm_mark_is_social(mark: Mapping[str, Any], text: str = "") -> bool:
    mark_type = str(mark.get("type", "")).casefold()
    attrs = mark.get("attrs", {})
    href = attrs.get("href", "") if isinstance(attrs, Mapping) else ""
    haystack = " ".join([mark_type, str(href), text]).casefold()
    return mark_type in {"link", "social"} and (
        any(word in haystack for word in _SOCIAL_WORDS)
        or bool(re.search(r"(?:^|[\s(\[])[@#][\w.-]+", text, flags=re.UNICODE))
    )


def _pm_entry_text(node: Mapping[str, Any]) -> str:
    parts: List[str] = []
    stopped = False

    def visit(current: Mapping[str, Any]) -> None:
        nonlocal stopped
        if stopped:
            return
        node_type = str(current.get("type", ""))
        if node_type in {"bulletList", "orderedList"}:
            return
        text = str(current.get("text", "")) if node_type == "text" else ""
        marks = current.get("marks", [])
        if text and isinstance(marks, list) and any(
            isinstance(mark, Mapping) and _pm_mark_is_social(mark, text) for mark in marks
        ):
            stopped = True
            return
        if text:
            parts.append(text)
        content = current.get("content", [])
        if isinstance(content, list):
            for child in content:
                if isinstance(child, Mapping):
                    visit(child)

    visit(node)
    return _strip_handle_suffix(" ".join(parts))


def _pm_node_is_struck(node: Mapping[str, Any]) -> bool:
    node_type = str(node.get("type", "")).casefold()
    if node_type in {"strike", "s", "del"}:
        return True
    marks = node.get("marks", [])
    if isinstance(marks, list):
        for mark in marks:
            if isinstance(mark, Mapping) and str(mark.get("type", "")).casefold() in {
                "strike",
                "strikethrough",
                "deleted",
            }:
                return True
    content = node.get("content", [])
    return isinstance(content, list) and any(
        isinstance(child, Mapping) and _pm_node_is_struck(child) for child in content
    )


def _extract_patreon_description_people(
    content: Sequence[Any],
    source_url: str,
) -> Tuple[List[str], List[str], List[str], Dict[str, Any]]:
    entries: List[Tuple[str, bool]] = []
    diagnostics: List[str] = []
    evidence: List[Dict[str, str]] = []
    direct_markers = 0
    unsafe_marker = False
    description_blocks = 0
    for index, node in enumerate(content):
        if not isinstance(node, Mapping) or node.get("type") != "paragraph":
            continue
        text = " ".join(_pm_node_text(node).split()).strip()
        normalized = _normalise_marker(text, remove_diacritics=True).rstrip(" :：\t")
        if _DESCRIPTION_PERSON_LABEL_RE.fullmatch(normalized):
            description_blocks += 1
            if index + 1 >= len(content) or not isinstance(content[index + 1], Mapping):
                diagnostics.append("description people paragraph has no following bulletList")
                continue
            bullet_list = content[index + 1]
            if bullet_list.get("type") != "bulletList":
                diagnostics.append("description people paragraph is not followed by a bulletList")
                continue
            items = bullet_list.get("content", [])
            if not isinstance(items, list):
                diagnostics.append("description people bulletList is malformed")
                continue
            block_entries = []
            for item in items:
                if not isinstance(item, Mapping) or item.get("type") != "listItem":
                    diagnostics.append("description people bulletList contains a non-listItem")
                    continue
                item_content = item.get("content", [])
                paragraphs = [
                    child for child in item_content
                    if isinstance(child, Mapping) and child.get("type") == "paragraph"
                ] if isinstance(item_content, list) else []
                if isinstance(item_content, list) and any(
                    isinstance(child, Mapping) and child.get("type") in {"bulletList", "orderedList"}
                    for child in item_content
                ):
                    unsafe_marker = True
                    diagnostics.append("description people listItem contains a nested list")
                    continue
                if not paragraphs:
                    diagnostics.append("description people listItem lacks a paragraph")
                    continue
                value = _pm_entry_text(paragraphs[0])
                if not value:
                    diagnostics.append("description people list contains an empty entry")
                    continue
                entries.append((value, _pm_node_is_struck(item)))
                block_entries.append(_safe_description_evidence(value))
            evidence.append({"marker": _safe_description_evidence(text), "entries": " | ".join(block_entries)})
            continue
        if not _description_marker_in_text(text):
            continue
        direct_markers += 1
        if "|" in text:
            unsafe_marker = True
            diagnostics.append("description people evidence contains a table delimiter")
            continue
        names = _description_sentence_names(text)
        if names:
            entries.extend((name, False) for name in names)
            evidence.append({"marker": "prose", "evidence": _safe_description_evidence(text)})
        else:
            diagnostics.append(
                "description people marker has no unambiguous name: "
                + _safe_description_evidence(text)[:300]
            )
    if description_blocks > 1:
        diagnostics.append("multiple description people blocks found; refusing ambiguous extraction")
        entries = []
        unsafe_marker = True
    people, excluded, diagnostics, identity_corrections = _normalise_description_entries(
        entries, source_url, diagnostics
    )
    if unsafe_marker:
        people = []
        identity_corrections = []
    if not people and not direct_markers and not evidence:
        return [], excluded, diagnostics, {}
    provenance = {
        "kind": "direct_source",
        "source_url": source_url,
        "description_evidence": evidence,
    } if evidence else {}
    if provenance and identity_corrections:
        provenance["description_identity_corrections"] = identity_corrections
    return people, excluded, diagnostics, provenance


def _merge_patreon_description_people(
    result: HostParseResult,
    content: Sequence[Any],
) -> HostParseResult:
    people, excluded, diagnostics, provenance = _extract_patreon_description_people(
        content, result.source_url
    )
    result.diagnostics.extend(diagnostics)
    if any("multiple description people blocks" in diagnostic for diagnostic in diagnostics):
        result.status = "ambiguous"
        result.hosts = []
        result.excluded_hosts = []
        result.provenance = {}
        return result
    existing_keys = {host_dedupe_key(host) for host in result.hosts}
    for person in people:
        key = host_dedupe_key(person)
        if key not in existing_keys:
            result.hosts.append(person)
            existing_keys.add(key)
    excluded_keys = {host_dedupe_key(host) for host in result.excluded_hosts}
    for person in excluded:
        key = host_dedupe_key(person)
        if key not in excluded_keys:
            result.excluded_hosts.append(person)
            excluded_keys.add(key)
    if people and result.status == "not_listed":
        result.status = "verified"
    if provenance:
        result.provenance = provenance
    return result


def parse_patreon_post_payload(payload: Any, source_url: str = "") -> HostParseResult:
    """Parse a Patreon ProseMirror/Tiptap ``content_json_string`` payload."""

    canonical_source, url_error = _source_url_or_error(source_url)
    if url_error:
        return HostParseResult("parse_error", diagnostics=[url_error])
    content_value, content_error = _payload_content_json(payload)
    if content_error:
        return HostParseResult("parse_error", diagnostics=[content_error], source_url=canonical_source)
    if isinstance(content_value, (str, bytes, bytearray)):
        try:
            document = json.loads(content_value)
        except (TypeError, ValueError):
            return HostParseResult(
                "parse_error",
                diagnostics=["Patreon content_json_string is invalid JSON"],
                source_url=canonical_source,
            )
    else:
        document = content_value
    if not isinstance(document, Mapping) or document.get("type") not in {"doc", "root"}:
        return HostParseResult(
            "parse_error",
            diagnostics=["Patreon content JSON is not a ProseMirror document"],
            source_url=canonical_source,
        )
    content = document.get("content", [])
    if not isinstance(content, list):
        return HostParseResult(
            "parse_error",
            diagnostics=["Patreon content JSON has no document content"],
            source_url=canonical_source,
        )

    candidates: List[Tuple[int, Mapping[str, Any]]] = []
    for index, node in enumerate(content):
        if not isinstance(node, Mapping) or node.get("type") != "paragraph":
            continue
        heading = _normalise_marker(_pm_node_text(node), remove_diacritics=True).rstrip(" :：")
        if heading == "prowadzacy":
            candidates.append((index, node))
    if not candidates:
        result = HostParseResult(
            "not_listed",
            diagnostics=["no structural Prowadzący block found"],
            source_url=canonical_source,
        )
        return _merge_patreon_description_people(result, content)
    if len(candidates) > 1:
        return HostParseResult(
            "parse_error",
            diagnostics=["multiple ambiguous Prowadzący blocks found"],
            source_url=canonical_source,
        )
    index, _ = candidates[0]
    if index + 1 >= len(content) or not isinstance(content[index + 1], Mapping):
        return HostParseResult(
            "parse_error",
            diagnostics=["Prowadzący paragraph has no following bulletList"],
            source_url=canonical_source,
        )
    bullet_list = content[index + 1]
    if bullet_list.get("type") != "bulletList":
        return HostParseResult(
            "parse_error",
            diagnostics=["Prowadzący paragraph is not followed by a bulletList"],
            source_url=canonical_source,
        )
    items = bullet_list.get("content", [])
    if not isinstance(items, list):
        return HostParseResult(
            "parse_error",
            diagnostics=["Patreon bulletList content is malformed"],
            source_url=canonical_source,
        )
    entries: List[Tuple[str, bool]] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "listItem":
            return HostParseResult(
                "parse_error",
                diagnostics=["Patreon bulletList contains a non-listItem"],
                source_url=canonical_source,
            )
        item_content = item.get("content", [])
        if not isinstance(item_content, list):
            return HostParseResult(
                "parse_error",
                diagnostics=["Patreon listItem content is malformed"],
                source_url=canonical_source,
            )
        paragraphs = [
            child
            for child in item_content
            if isinstance(child, Mapping) and child.get("type") == "paragraph"
        ]
        if any(
            isinstance(child, Mapping) and child.get("type") in {"bulletList", "orderedList"}
            for child in item_content
        ):
            return HostParseResult(
                "parse_error",
                diagnostics=["Patreon listItem contains a nested list"],
                source_url=canonical_source,
            )
        if not paragraphs or any(
            not isinstance(child, Mapping)
            or child.get("type") not in {"paragraph", "bulletList", "orderedList"}
            for child in item_content
        ):
            return HostParseResult(
                "parse_error",
                diagnostics=["Patreon listItem lacks a valid paragraph"],
                source_url=canonical_source,
            )
        value = _pm_entry_text(paragraphs[0])
        if not value:
            return HostParseResult(
                "parse_error",
                diagnostics=["Patreon host list contains an empty entry"],
                source_url=canonical_source,
            )
        entries.append((value, _pm_node_is_struck(item)))
    return _merge_patreon_description_people(
        _normalise_entries(entries, canonical_source),
        content,
    )


def canonical_url(value: str) -> str:
    """Return a credential-free, HTTPS-only canonical URL."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL is empty")
    raw_value = value.strip()
    parts = urlsplit(raw_value)
    if parts.scheme.casefold() != "https":
        raise ValueError("HTTPS URL required")
    if parts.username or parts.password:
        raise ValueError("URL credentials are not permitted")
    if "?" in raw_value or parts.query:
        raise ValueError("URL query is not permitted")
    if "#" in raw_value or parts.fragment:
        raise ValueError("URL fragment is not permitted")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    if not hostname:
        raise ValueError("URL hostname is missing")
    try:
        hostname = hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError as exc:
        raise ValueError("URL hostname is invalid") from exc
    if ":" in hostname and not hostname.startswith("["):
        netloc = "[" + hostname + "]"
    else:
        netloc = hostname
    if port is not None and port != 443:
        netloc += ":" + str(port)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", netloc, path, parts.query, ""))


def create_ssl_context() -> ssl.SSLContext:
    """Create an explicitly certificate- and hostname-validating context."""

    context = ssl.create_default_context()
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


def _header_value(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is not None:
            return str(value)
    if isinstance(headers, Mapping):
        wanted = name.casefold()
        for key, value in headers.items():
            if str(key).casefold() == wanted:
                return str(value)
    return ""


def _response_status(response: Any) -> Optional[int]:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _response_url(response: Any, fallback: str = "") -> str:
    if hasattr(response, "geturl"):
        try:
            value = response.geturl()
            if value:
                return str(value)
        except Exception:
            pass
    value = getattr(response, "url", "")
    return str(value or fallback)


def _content_type(headers: Any) -> str:
    return _header_value(headers, "Content-Type").strip()


def decode_response_body(body: bytes, content_type: str = "") -> str:
    """Decode bytes using the declared charset, with a UTF-8 fallback."""

    if isinstance(body, str):
        return body
    charset = ""
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, flags=re.I)
    if match:
        charset = match.group(1)
    encoding = charset or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        return body.decode("utf-8", errors="replace")


def _body_plain_text(body: str) -> str:
    document, _ = _parse_html_tree(body)
    return _normalise_marker(_node_text(document))


def _same_url_resource(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    return (
        left_parts.scheme.casefold() == right_parts.scheme.casefold()
        and left_parts.netloc.casefold() == right_parts.netloc.casefold()
        and (left_parts.path.rstrip("/") or "/") == (right_parts.path.rstrip("/") or "/")
    )


def validate_response(
    response: Any,
    body: Any = b"",
    expected_url: str = "",
    expected_title: str = "",
    expected_episode: str = "",
) -> ResponseValidationResult:
    """Validate status, HTML content type, HTTPS final URL, and page markers."""

    status_code = _response_status(response)
    content_type = _content_type(getattr(response, "headers", None))
    raw_final_url = _response_url(response, expected_url)
    diagnostics: List[str] = []
    if status_code is None or not 200 <= status_code < 300:
        diagnostics.append("HTTP response status is not successful")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        diagnostics.append("response content type is not HTML")
    try:
        final_url = canonical_url(raw_final_url)
    except ValueError:
        final_url = ""
        diagnostics.append("final URL is not valid HTTPS")
    if expected_url:
        try:
            canonical_expected = canonical_url(expected_url)
        except ValueError:
            canonical_expected = ""
            diagnostics.append("expected URL is not valid HTTPS")
        if final_url and canonical_expected and not _same_url_resource(final_url, canonical_expected):
            diagnostics.append("final URL does not match expected resource")
    decoded_body = body if isinstance(body, str) else decode_response_body(body, content_type)
    plain_text = _body_plain_text(decoded_body)
    if any(marker in plain_text for marker in {"page not found", "404 not found", "nie znaleziono strony"}):
        diagnostics.append("response looks like a soft 404")
    if expected_title and _normalise_marker(expected_title) not in plain_text:
        diagnostics.append("expected title marker not found")
    if expected_episode and _normalise_marker(expected_episode) not in plain_text and not _title_body_marker_present(expected_title, plain_text):
        diagnostics.append("expected episode marker not found")
    return ResponseValidationResult(
        not diagnostics,
        diagnostics,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
    )


def retry_after_seconds(value: str, now: Optional[datetime] = None) -> Optional[float]:
    """Parse integer or HTTP-date Retry-After values without raising."""

    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (when - current).total_seconds())


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_https_html(
    url: str,
    expected_url: str = "",
    expected_title: str = "",
    expected_episode: str = "",
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    retries: int = 2,
    opener: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """Fetch one public HTML page with bounded, certificate-validating HTTPS."""

    try:
        source_url = canonical_url(url)
    except ValueError as exc:
        return FetchResult("fetch_error", source_url="", diagnostics=["invalid URL: " + str(exc)])
    if max_bytes <= 0:
        return FetchResult("fetch_error", source_url=source_url, diagnostics=["body limit must be positive"])
    retries = max(0, int(retries))
    request_url = _rrn_request_url(source_url)
    request = Request(
        request_url,
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9",
            "User-Agent": "Nadgryzieni-host-audit/1.0",
        },
    )
    context = create_ssl_context()
    if opener is None:
        no_redirect_opener = build_opener(_NoRedirectHandler(), HTTPSHandler(context=context))

        def open_fn(request: Request, *, context: ssl.SSLContext, timeout: float):
            return no_redirect_opener.open(request, timeout=timeout)
    else:
        open_fn = opener
    last_diagnostics: List[str] = []
    for attempt in range(retries + 1):
        response = None
        try:
            response = open_fn(request, context=context, timeout=timeout)
            status_code = _response_status(response)
            headers = getattr(response, "headers", None)
            retryable = status_code == 429 or (status_code is not None and 500 <= status_code < 600)
            if retryable and attempt < retries:
                delay = retry_after_seconds(_header_value(headers, "Retry-After"))
                if delay is None:
                    delay = min(2.0 ** attempt, 8.0)
                sleep_fn(delay)
                continue
            body_bytes = response.read(max_bytes + 1)
            if len(body_bytes) > max_bytes:
                return FetchResult(
                    "fetch_error",
                    source_url=source_url,
                    final_url=_response_url(response, source_url),
                    status_code=status_code,
                    content_type=_content_type(headers),
                    diagnostics=["response body exceeds bounded size limit"],
                )
            content_type = _content_type(headers)
            body = decode_response_body(body_bytes, content_type)
            validation = validate_response(
                response,
                body,
                expected_url=expected_url or source_url,
                expected_title=expected_title,
                expected_episode=expected_episode,
            )
            if not validation.ok:
                return FetchResult(
                    "fetch_error",
                    source_url=source_url,
                    final_url=validation.final_url,
                    status_code=validation.status_code,
                    content_type=validation.content_type,
                    diagnostics=list(validation.diagnostics),
                )
            return FetchResult(
                "ok",
                body=body,
                source_url=source_url,
                final_url=validation.final_url,
                status_code=validation.status_code,
                content_type=validation.content_type,
                diagnostics=list(validation.diagnostics),
            )
        except HTTPError as exc:
            status_code = getattr(exc, "code", None)
            headers = getattr(exc, "headers", None)
            last_diagnostics = ["HTTP request failed"]
            if status_code == 429 or (status_code is not None and 500 <= status_code < 600):
                if attempt < retries:
                    delay = retry_after_seconds(_header_value(headers, "Retry-After"))
                    sleep_fn(delay if delay is not None else min(2.0 ** attempt, 8.0))
                    continue
            break
        except (OSError, URLError, ssl.SSLError, ValueError):
            last_diagnostics = ["HTTPS request failed"]
            if attempt < retries:
                sleep_fn(min(2.0 ** attempt, 8.0))
                continue
            break
        finally:
            if response is not None and hasattr(response, "close"):
                try:
                    response.close()
                except Exception:
                    pass
    return FetchResult(
        "fetch_error",
        source_url=source_url,
        diagnostics=last_diagnostics or ["HTTPS request failed"],
    )


def _record_identity_fields(record: Mapping[str, Any]) -> Dict[str, str]:
    guid = record.get("source_guid") or record.get("guid") or ""
    url = record.get("source_url") or record.get("url") or ""
    fields: Dict[str, str] = {}
    if guid:
        fields["source_guid"] = " ".join(str(guid).split())
    else:
        fields["source_url"] = canonical_url(str(url))
        fields["episode"] = " ".join(str(record.get("episode", record.get("episode_number", ""))).split())
        fields["title"] = " ".join(str(record.get("title", "")).split())
        fields["date"] = " ".join(str(record.get("date", "")).split())
        fields["duration"] = " ".join(str(record.get("duration", "")).split())
    return fields


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_key(record: Mapping[str, Any]) -> str:
    """Build a deterministic identity digest without using host fields."""

    return _stable_digest(_record_identity_fields(record))


def dataset_fingerprint(records: Iterable[Mapping[str, Any]]) -> str:
    """Fingerprint canonical record identity fields, excluding host metadata."""

    rows = [
        {"record_key": record_key(record), "fields": _record_identity_fields(record)}
        for record in records
    ]
    rows.sort(key=lambda row: row["record_key"])
    return _stable_digest(rows)


def parse_result_to_dict(result: HostParseResult) -> Dict[str, Any]:
    return result.to_dict()


def serialize_parse_result(result: HostParseResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


# ── Historical audit/apply workflow ──────────────────────────────────────────

AUDIT_SCHEMA_VERSION = 1
PARSER_VERSION = "nadgryzieni-hosts/2.2"
AUDIT_USER_AGENT = "Nadgryzieni-host-audit/1.0"
UNRESOLVED_STATUSES = frozenset({"unavailable", "ambiguous", "manual_review"})
DEFAULT_HOST_CACHE_PATH = Path(os.environ.get(
    "NADGRYZIENI_HOST_CACHE",
    str(Path.home() / ".hermes" / "profiles" / "r2-d2" / "state" / "nadgryzieni-host-cache.json"),
))


def _pipeline_module() -> Any:
    """Import pipeline helpers lazily to keep parser imports side-effect free."""
    import nadgryzieni_pipeline as pipeline
    return pipeline


def _load_current_rows() -> Tuple[Any, List[Dict[str, Any]], Dict[str, Any]]:
    pipeline = _pipeline_module()
    current_data: Dict[str, Any] = {}
    current_data_bytes = pipeline._read_bytes_secure(
        pipeline.DATA_JSON_PATH,
        pipeline.REPO_DIR.parent,
    )
    if current_data_bytes is not None:
        current_data = json.loads(current_data_bytes.decode("utf-8"))
    rows, _ = pipeline.parse_archive(pipeline.ARCHIVE_PATH)
    pipeline.attach_existing_data(rows, current_data)
    for row in rows:
        row["record_key"] = pipeline.build_record_key(row)
    keys = [row["record_key"] for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Current archive contains duplicate record keys")
    return pipeline, rows, current_data


def _numeric_episode(value: Any) -> Optional[float]:
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*$", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _is_afterparty(row: Mapping[str, Any]) -> bool:
    number = _numeric_episode(row.get("episode"))
    title = str(row.get("title", "")).casefold()
    return "(afterparty)" in title or (
        number is not None and not number.is_integer() and "(afterparty)" in title
    )


def _pair_base_episode(row: Mapping[str, Any]) -> Optional[str]:
    number = _numeric_episode(row.get("episode"))
    if number is None:
        return None
    return str(int(number)) if not number.is_integer() else str(int(number))


def _afterparty_uses_pairing_rule(row: Mapping[str, Any]) -> bool:
    number = _numeric_episode(row.get("episode"))
    return _is_afterparty(row) and number is not None and number >= 550


def _is_rrn_url(url: str) -> bool:
    return urlsplit(str(url or "")).netloc.casefold() in {
        "retrorocketnetwork.pl",
        "www.retrorocketnetwork.pl",
    }


def _rrn_request_url(canonical: str) -> str:
    parts = urlsplit(canonical)
    if not _is_rrn_url(canonical) or len(parts.path) <= 1:
        return canonical
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/", parts.query, ""))


def _is_patreon_url(url: str) -> bool:
    return "patreon.com" in urlsplit(str(url or "")).netloc.casefold()


def _robots_allowed(url: str, cache: Dict[str, bool]) -> bool:
    """Read robots.txt once per origin and fail closed if policy is unclear."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin in cache:
        return cache[origin]
    robots_url = origin + "/robots.txt"
    request = Request(robots_url, headers={"User-Agent": AUDIT_USER_AGENT, "Accept": "text/plain"})
    try:
        opener = build_opener(_NoRedirectHandler(), HTTPSHandler(context=create_ssl_context()))
        with opener.open(request, timeout=15) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status == 404:
                cache[origin] = True
                return True
            if status is None or not 200 <= int(status) < 300:
                cache[origin] = False
                return False
            body = response.read(256_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        cache[origin] = getattr(exc, "code", 0) == 404
        return cache[origin]
    except (OSError, URLError, ssl.SSLError):
        cache[origin] = False
        return False
    rules: Dict[str, List[str]] = {"*": []}
    active = False
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        key = key.casefold()
        if key == "user-agent":
            active = value == "*" or value.casefold() == AUDIT_USER_AGENT.casefold()
        elif key == "disallow" and active and value:
            rules.setdefault("*", []).append(value)
    path = parts.path or "/"
    allowed = not any(path.startswith(prefix) for prefix in rules.get("*", []))
    cache[origin] = allowed
    return allowed


def _direct_audit_entry(
    row: Mapping[str, Any],
    fetch_cache: Dict[str, FetchResult],
    robots_cache: Dict[str, bool],
    last_fetch: List[float],
    rate_limit: float,
) -> Dict[str, Any]:
    pipeline = _pipeline_module()
    url = str(row.get("url") or "").strip()
    source = "rrn" if _is_rrn_url(url) else "patreon" if _is_patreon_url(url) else "manual"
    try:
        source_url = canonical_url(url) if url else ""
    except ValueError:
        source_url = ""
    base: Dict[str, Any] = {
        "record_key": row["record_key"],
        "episode": str(row.get("episode", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "duration": str(row.get("duration", "")),
        "hosts": [],
        "hosts_status": "manual_review",
        "hosts_source": source,
        "hosts_source_url": source_url,
        "provenance": {"kind": "direct_source", "source_url": source_url},
        "diagnostics": [],
    }
    if not url or source != "rrn":
        base["hosts_status"] = "unavailable" if source == "patreon" else "manual_review"
        base["diagnostics"] = ["source is not a directly auditable RRN HTML page"]
        return base
    try:
        canonical = canonical_url(url)
    except ValueError as exc:
        base["hosts_status"] = "manual_review"
        base["diagnostics"] = ["invalid source URL: " + str(exc)]
        return base
    base["hosts_source_url"] = canonical
    base["provenance"]["source_url"] = canonical
    if not _robots_allowed(canonical, robots_cache):
        base["hosts_status"] = "unavailable"
        base["diagnostics"] = ["robots policy denied or could not be read"]
        return base
    if canonical not in fetch_cache:
        wait = rate_limit - (time.monotonic() - last_fetch[0])
        if wait > 0:
            time.sleep(wait)
        fetch_cache[canonical] = fetch_https_html(
            canonical,
            expected_url=canonical,
            expected_title=str(row.get("title", "")),
            expected_episode=str(row.get("episode", "")),
        )
        last_fetch[0] = time.monotonic()
    fetched = fetch_cache[canonical]
    if fetched.status != "ok":
        base["hosts_status"] = "unavailable"
        base["diagnostics"] = list(fetched.diagnostics)
        base["fetch"] = {
            "status": fetched.status,
            "status_code": fetched.status_code,
            "content_type": fetched.content_type,
            "final_url": fetched.final_url,
        }
        return base
    parsed = parse_rrn_hosts(
        fetched.body,
        expected_url=canonical,
        expected_title=str(row.get("title", "")),
        expected_episode=str(row.get("episode", "")),
    )
    base["hosts"] = list(parsed.hosts)
    base["hosts_status"] = parsed.status if parsed.status in {"verified", "not_listed"} else "ambiguous"
    base["provenance"] = parsed.provenance or {
        "kind": "direct_source",
        "source_url": canonical,
    }
    base["diagnostics"] = list(parsed.diagnostics)
    if parsed.excluded_hosts:
        base["excluded_hosts"] = list(parsed.excluded_hosts)
    base["fetch"] = {
        "status": fetched.status,
        "status_code": fetched.status_code,
        "content_type": fetched.content_type,
        "final_url": fetched.final_url,
    }
    if parsed.status not in {"verified", "not_listed"}:
        base["diagnostics"].append("structural parser did not produce a publishable result")
    return apply_record_host_corrections(str(row["record_key"]), base)


def _reject_symlink_components(path: Path, stop_at: Path) -> None:
    current = Path(path)
    boundary = Path(stop_at)
    if boundary not in {current, *current.parents}:
        boundary = current.parent.parent
    while True:
        if current.is_symlink():
            raise RuntimeError(f"Refusing unsafe symlink path component: {current}")
        if current == boundary:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_cache_bytes(path: Path) -> bytes | None:
    path_abs = Path(os.path.abspath(os.fspath(path)))
    root_abs = Path(os.path.abspath(os.fspath(path.parent.parent)))
    try:
        path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise RuntimeError(f"Host cache path is outside its root: {path_abs}") from exc
    _reject_symlink_components(path_abs, root_abs)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root_abs, flags)
    try:
        for component in path_abs.parent.relative_to(root_abs).parts:
            if component in {"", "."}:
                continue
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            descriptor = os.open(
                path_abs.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RuntimeError("Host cache is not a regular file")
        with os.fdopen(descriptor, "rb") as source:
            return source.read()
    finally:
        os.close(parent_fd)


def _load_host_cache(path: Path) -> Dict[str, Any]:
    try:
        content = _read_cache_bytes(path)
        if content is None:
            return {}
        payload = json.loads(content.decode("utf-8"))
    except RuntimeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") != AUDIT_SCHEMA_VERSION or payload.get("parser_version") != PARSER_VERSION:
        return {}
    return payload.get("records", {}) if isinstance(payload.get("records"), dict) else {}


def _atomic_cache_write(path: Path, content: bytes, root: Path) -> None:
    path_abs = Path(os.path.abspath(os.fspath(path)))
    root_abs = Path(os.path.abspath(os.fspath(root)))
    try:
        path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise RuntimeError(f"Host cache path is outside its root: {path_abs}") from exc
    _reject_symlink_components(path_abs, root_abs)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root_abs, flags)
    temporary_name = None
    try:
        for component in path_abs.parent.relative_to(root_abs).parts:
            if component in {"", "."}:
                continue
            try:
                next_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=parent_fd)
                next_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            existing_stat = os.stat(path_abs.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_stat = None
        if existing_stat is not None:
            if stat.S_ISLNK(existing_stat.st_mode):
                raise RuntimeError(f"Refusing symlink host-cache destination: {path_abs}")
            if not stat.S_ISREG(existing_stat.st_mode):
                raise RuntimeError(f"Unsafe host-cache destination: {path_abs}")
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _ in range(10):
            candidate = f".{path_abs.name}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(candidate, create_flags, 0o600, dir_fd=parent_fd)
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Could not allocate a unique host-cache temporary file")
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o600)
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path_abs.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _save_host_cache(path: Path, records: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "records": {key: records[key] for key in sorted(records)},
    }
    _atomic_cache_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        path.parent.parent,
    )


def _sanitize_public_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, child in value.items():
            safe_key = _sanitize_public_key(key)
            sanitized[safe_key] = "[REDACTED]" if safe_key == "[REDACTED_KEY]" else _sanitize_public_value(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_public_value(child) for child in value]
    if isinstance(value, str):
        return _safe_description_evidence(value)
    return value


def _safe_report_source_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return canonical_url(text)
    except ValueError:
        return ""


def _sanitize_audit_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    original_records = report.get("records") if isinstance(report, Mapping) else None
    safe_record_urls: Dict[str, str] = {}
    safe_provenance_urls: Dict[str, str] = {}
    safe_identity_fields: Dict[str, Dict[str, Any]] = {}
    provenance_has_source_url: set[str] = set()
    if isinstance(original_records, Mapping):
        for key, original_record in original_records.items():
            if not isinstance(original_record, Mapping):
                continue
            record_key = str(key)
            safe_identity_fields[record_key] = {
                field: original_record.get(field)
                for field in ("record_key", "episode", "title", "date", "duration")
            }
            safe_record_urls[record_key] = _safe_report_source_url(
                original_record.get("hosts_source_url")
            )
            original_provenance = original_record.get("provenance")
            if isinstance(original_provenance, Mapping) and "source_url" in original_provenance:
                provenance_has_source_url.add(record_key)
                safe_provenance_urls[record_key] = _safe_report_source_url(
                    original_provenance.get("source_url")
                )

    sanitized = _sanitize_public_value(dict(report))
    if not isinstance(sanitized, dict):
        raise ValueError("Host audit report is not an object")
    records = sanitized.get("records")
    if isinstance(records, dict):
        for record_key, safe_url in safe_record_urls.items():
            record = records.get(record_key)
            if not isinstance(record, dict):
                continue
            record.update(safe_identity_fields.get(record_key, {}))
            record["hosts_source_url"] = safe_url
            if record_key in provenance_has_source_url:
                provenance = record.get("provenance")
                if isinstance(provenance, dict):
                    provenance["source_url"] = safe_provenance_urls.get(record_key, "")
    fetch_targets = report.get("fetch_targets") if isinstance(report, Mapping) else None
    if isinstance(fetch_targets, list):
        sanitized["fetch_targets"] = [
            safe_url
            for value in fetch_targets
            for safe_url in [_safe_report_source_url(value)]
            if safe_url
        ]
    return sanitized


def _safe_public_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return canonical_url(text)
    except ValueError as exc:
        raise RuntimeError("Unsafe cached provenance URL") from exc


def _cache_projection(result: Mapping[str, Any]) -> Dict[str, Any]:
    source_url = _safe_public_url(result.get("hosts_source_url", ""))
    provenance = _sanitize_public_value(dict(result.get("provenance", {})))
    if not isinstance(provenance, dict):
        raise RuntimeError("Cached provenance is not an object")
    if "source_url" in provenance:
        provenance["source_url"] = source_url
    return {
        "hosts": [normalize_host_name(host) for host in result.get("hosts", [])],
        "hosts_status": result.get("hosts_status"),
        "hosts_source": result.get("hosts_source", "rrn"),
        "hosts_source_url": source_url,
        "provenance": provenance,
        "diagnostics": list(result.get("diagnostics", [])),
        "fetch": dict(result.get("fetch", {})),
    }


def _cached_direct_entry(row: Mapping[str, Any], cached: Mapping[str, Any]) -> Dict[str, Any]:
    source_url = _safe_public_url(cached.get("hosts_source_url") or row.get("url") or "")
    provenance = _sanitize_public_value(dict(cached.get("provenance", {
        "kind": "direct_source",
        "source_url": source_url,
    })))
    if not isinstance(provenance, dict):
        raise RuntimeError("Cached provenance is not an object")
    provenance["source_url"] = source_url
    entry = {
        "record_key": row["record_key"],
        "episode": str(row.get("episode", "")),
        "title": str(row.get("title", "")),
        "date": str(row.get("date", "")),
        "duration": str(row.get("duration", "")),
        "hosts": [normalize_host_name(host) for host in cached.get("hosts", [])],
        "hosts_status": cached.get("hosts_status", "manual_review"),
        "hosts_source": cached.get("hosts_source", "rrn"),
        "hosts_source_url": source_url,
        "provenance": provenance,
        "diagnostics": list(cached.get("diagnostics", [])),
        "fetch": dict(cached.get("fetch", {})),
    }
    return apply_record_host_corrections(str(row["record_key"]), entry)


def audit_repository(
    output: Path,
    rate_limit: float = 0.25,
    cache_path: Path = DEFAULT_HOST_CACHE_PATH,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Audit all current records while holding the shared publication lock."""
    pipeline = _pipeline_module()
    lock_owned_here = not pipeline._pipeline_lock_owned_by_current_thread()
    if lock_owned_here and not pipeline.acquire_pipeline_lock():
        raise RuntimeError("Another Nadgryzieni publication is active")
    try:
        return _audit_repository_locked(output, rate_limit, cache_path, refresh)
    finally:
        if lock_owned_here:
            pipeline.release_pipeline_lock()


def _audit_repository_locked(
    output: Path,
    rate_limit: float = 0.25,
    cache_path: Path = DEFAULT_HOST_CACHE_PATH,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Locked implementation of :func:`audit_repository`."""
    pipeline, rows, _ = _load_current_rows()
    fetch_cache: Dict[str, FetchResult] = {}
    robots_cache: Dict[str, bool] = {}
    last_fetch = [0.0]
    results: Dict[str, Dict[str, Any]] = {}
    cache_records = {} if refresh else _load_host_cache(cache_path)
    cache_hit_targets = set()
    direct_targets = set()
    main_by_episode: Dict[str, Mapping[str, Any]] = {
        str(row.get("episode")): row
        for row in rows
        if not _is_afterparty(row) and _numeric_episode(row.get("episode")) is not None
    }
    for row in rows:
        base_episode = _pair_base_episode(row)
        has_main_pair = base_episode in main_by_episode if base_episode else False
        direct_rrn_fallback = _afterparty_uses_pairing_rule(row) and not has_main_pair and _is_rrn_url(row.get("url", ""))
        if _afterparty_uses_pairing_rule(row) and not direct_rrn_fallback:
            continue
        canonical = canonical_url(row.get("url", "")) if _is_rrn_url(row.get("url", "")) else ""
        if canonical:
            direct_targets.add(canonical)
        cached = cache_records.get(canonical) if canonical else None
        if cached:
            cache_hit_targets.add(canonical)
            results[row["record_key"]] = _cached_direct_entry(row, cached)
        else:
            result = _direct_audit_entry(
                row, fetch_cache, robots_cache, last_fetch, max(0.0, float(rate_limit))
            )
            results[row["record_key"]] = result
            if canonical and result.get("hosts_status") in {"verified", "not_listed"}:
                cache_records[canonical] = _cache_projection(result)
                _save_host_cache(cache_path, cache_records)
    for row in rows:
        if not _afterparty_uses_pairing_rule(row):
            continue
        key = row["record_key"]
        base_episode = _pair_base_episode(row)
        main = main_by_episode.get(base_episode or "")
        if main is None and _is_rrn_url(row.get("url", "")):
            canonical = canonical_url(row.get("url", ""))
            direct_targets.add(canonical)
            cached = cache_records.get(canonical) if not refresh else None
            if cached:
                cache_hit_targets.add(canonical)
                results.setdefault(key, _cached_direct_entry(row, cached))
            else:
                result = _direct_audit_entry(
                    row, fetch_cache, robots_cache, last_fetch, max(0.0, float(rate_limit))
                )
                results.setdefault(key, result)
                if result.get("hosts_status") in {"verified", "not_listed"}:
                    cache_records[canonical] = _cache_projection(result)
                    _save_host_cache(cache_path, cache_records)
            continue
        main_result = results.get(main.get("record_key", "") if main else "")
        result: Dict[str, Any] = {
            "record_key": key,
            "episode": str(row.get("episode", "")),
            "title": str(row.get("title", "")),
            "date": str(row.get("date", "")),
            "duration": str(row.get("duration", "")),
            "hosts": [],
            "hosts_status": "manual_review",
            "hosts_source": "paired_rrn",
            "hosts_source_url": "",
            "provenance": {
                "kind": "paired_rrn",
                "rule": "afterparty_same_hosts_from_main",
            },
            "diagnostics": [],
        }
        if not main or not main_result:
            result["diagnostics"] = ["corresponding main RRN record was not found"]
        elif main_result.get("hosts_status") in {"verified", "not_listed"}:
            result["hosts"] = list(main_result.get("hosts", []))
            result["hosts_status"] = main_result["hosts_status"]
            result["hosts_source_url"] = main_result.get("hosts_source_url", "")
            result["provenance"].update({
                "paired_record_key": main_result["record_key"],
                "paired_episode": str(main.get("episode", "")),
            })
            result["diagnostics"] = [
                "hosts explicitly inherited from corresponding main RRN record by approved rule"
            ]
        else:
            result["diagnostics"] = [
                "corresponding main RRN host result is unresolved: "
                + str(main_result.get("hosts_status", ""))
            ]
            result["provenance"]["paired_record_key"] = main_result["record_key"]
        results[key] = result

    fetch_targets = sorted(direct_targets)
    _save_host_cache(cache_path, cache_records)
    report: Dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_fingerprint": pipeline.dataset_fingerprint(rows),
        "record_count": len(rows),
        "fetch_target_count": len(fetch_targets),
        "network_fetch_count": len(fetch_cache),
        "cache_hit_count": len(cache_hit_targets),
        "fetch_targets": fetch_targets,
        "records": {key: results[key] for key in sorted(results)},
    }
    if len(results) != len(rows):
        raise ValueError("Host audit did not produce one result per record")
    report = _sanitize_audit_report(report)
    pipeline._atomic_write_text(
        output,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        root=output.parent.parent,
        mode=0o600,
    )
    return report


def _validate_audit_against_current(report: Mapping[str, Any], rows: List[Dict[str, Any]]) -> None:
    pipeline = _pipeline_module()
    if report.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise ValueError("Host audit has an unsupported schema")
    if report.get("parser_version") != PARSER_VERSION:
        raise ValueError("Host audit was generated by a different parser version")
    if report.get("dataset_fingerprint") != pipeline.dataset_fingerprint(rows):
        raise ValueError("Host audit is stale: dataset fingerprint does not match current archive")
    records = report.get("records")
    if not isinstance(records, Mapping):
        raise ValueError("Host audit has no per-record results")
    current_keys = {row["record_key"] for row in rows}
    audit_keys = {str(key) for key in records}
    if audit_keys != current_keys:
        missing = sorted(current_keys - audit_keys)
        orphaned = sorted(audit_keys - current_keys)
        raise ValueError(f"Host audit record set mismatch; missing={missing[:3]}, orphaned={orphaned[:3]}")

    rows_by_key = {row["record_key"]: row for row in rows}
    for record_key, row in rows_by_key.items():
        result = records[record_key]
        if not isinstance(result, Mapping):
            raise ValueError(f"Host audit result for {record_key} is not an object")
        if str(result.get("record_key") or "") != record_key:
            raise ValueError(f"Host audit result for {record_key} has a mismatched record key")
        for field in ("episode", "title", "date", "duration"):
            if pipeline.normalize_identity_text(result.get(field, "")) != pipeline.normalize_identity_text(row.get(field, "")):
                raise ValueError(f"Host audit result for {record_key} has a mismatched {field}")
        result_source_url = str(result.get("hosts_source_url") or "").strip()
        try:
            result_source_canonical = canonical_url(result_source_url)
        except ValueError as exc:
            raise ValueError(f"Host audit result for {record_key} has an invalid source URL: {exc}") from exc
        if str(result.get("hosts_source") or "") != "paired_rrn":
            try:
                row_source_canonical = canonical_url(str(row.get("url") or ""))
            except ValueError as exc:
                raise ValueError(f"Current row {record_key} has an invalid source URL: {exc}") from exc
            if result_source_canonical != row_source_canonical:
                raise ValueError(f"Host audit result for {record_key} has a mismatched source URL")
        provenance = result.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"Host audit result for {record_key} has invalid provenance")
        if result.get("hosts_source") == "paired_rrn":
            paired_key = str(provenance.get("paired_record_key") or "")
            if paired_key not in current_keys or paired_key == record_key:
                raise ValueError(f"Host audit result for {record_key} has an invalid paired record key")


def _publishable_manifest(manifest: Mapping[str, Any]) -> None:
    unresolved = [
        key for key, entry in manifest.get("records", {}).items()
        if entry.get("hosts_status") in UNRESOLVED_STATUSES
    ]
    if unresolved:
        raise ValueError(f"Host metadata has unresolved records: {', '.join(sorted(unresolved)[:5])}")


def apply_audit(audit_path: Path, dry_run: bool = False, write: bool = False) -> Dict[str, Any]:
    """Apply a matching audit through staged archive/data/manifest outputs."""
    if dry_run == write:
        raise ValueError("Choose exactly one of --dry-run or --write")
    pipeline = _pipeline_module()
    audit_content = pipeline._read_bytes_secure(audit_path, audit_path.parent.parent)
    if audit_content is None:
        raise FileNotFoundError(audit_path)
    raw_report = json.loads(audit_content.decode("utf-8"))
    if not isinstance(raw_report, Mapping):
        raise ValueError("Host audit report is not an object")
    report = _sanitize_audit_report(raw_report)

    def prepare_application(current_pipeline: Any, rows: List[Dict[str, Any]]) -> Tuple[dict, dict, str, dict]:
        _validate_audit_against_current(report, rows)
        audit_records = report["records"]
        for row in rows:
            result = dict(audit_records[row["record_key"]])
            row.update({
                "hosts": list(result.get("hosts", [])),
                "hosts_status": result.get("hosts_status", ""),
                "hosts_source": result.get("hosts_source", ""),
                "hosts_source_url": result.get("hosts_source_url", ""),
                "hosts_provenance": result.get("provenance"),
            })
        manifest = current_pipeline.manifest_from_rows(rows, strict=True)
        for key, result in audit_records.items():
            entry = manifest["records"][key]
            if result.get("diagnostics"):
                entry["diagnostics"] = list(result["diagnostics"])
            entry["audit"] = {
                "parser_version": report.get("parser_version"),
                "dataset_fingerprint": report.get("dataset_fingerprint"),
            }
        _publishable_manifest(manifest)
        data = current_pipeline.generate_data_json(rows)
        current_pipeline.validate_generated_data(data, rows)
        statistics_markdown = current_pipeline.generate_statistics(rows)
        stats = {
            "records": len(rows),
            "verified": sum(1 for row in rows if row.get("hosts_status") == "verified"),
            "not_listed": sum(1 for row in rows if row.get("hosts_status") == "not_listed"),
            "fetch_targets": report.get("fetch_target_count", 0),
            "mode": "dry-run" if dry_run else "write",
        }
        return manifest, data, statistics_markdown, stats

    if dry_run:
        pipeline, rows, _ = _load_current_rows()
        manifest, data, statistics_markdown, stats = prepare_application(pipeline, rows)
        pipeline.write_archive(rows, dry=True)
        pipeline.write_data_json(data, dry=True)
        pipeline.write_host_metadata(manifest, dry=True, record_rows=rows)
        pipeline.update_readme(data, dry=True)
        dry_stats_path = pipeline.STATS_PATH.with_name(
            f"{pipeline.STATS_PATH.stem}.dry{pipeline.STATS_PATH.suffix}"
        )
        pipeline._atomic_write_text(
            dry_stats_path,
            statistics_markdown,
            root=pipeline.REPO_DIR.parent,
            mode=0o644,
        )
        return stats

    lock_pipeline = pipeline
    if not lock_pipeline.acquire_pipeline_lock():
        raise RuntimeError("Another Nadgryzieni publication is active")
    stage_dir = None
    try:
        pipeline, rows, _ = _load_current_rows()
        manifest, data, statistics_markdown, stats = prepare_application(pipeline, rows)
        repo = pipeline.REPO_DIR
        stage_dir = Path(tempfile.mkdtemp(prefix=".nadgryzieni-hosts-stage-", dir=str(repo.parent)))
        assert stage_dir is not None
        staged_archive = stage_dir / pipeline.ARCHIVE_PATH.name
        staged_data = stage_dir / pipeline.DATA_JSON_PATH.name
        staged_manifest = stage_dir / pipeline.HOST_METADATA_PATH.name
        staged_readme = stage_dir / pipeline.README_PATH.name
        staged_stats = stage_dir / pipeline.STATS_PATH.name
        pipeline.write_archive(rows, target_path=staged_archive)
        pipeline.write_data_json(data, target_path=staged_data)
        pipeline.write_host_metadata(manifest, path=staged_manifest, record_rows=rows)
        pipeline.update_readme(data, target_path=staged_readme)
        pipeline._atomic_write_text(
            staged_stats,
            statistics_markdown,
            root=stage_dir.parent,
            mode=0o644,
        )
        staged_data_content = pipeline._read_bytes_secure(staged_data, repo.parent)
        if staged_data_content is None:
            raise FileNotFoundError(staged_data)
        staged_data_payload = json.loads(staged_data_content.decode("utf-8"))
        pipeline.validate_generated_data(staged_data_payload, rows)
        staged_rows, _ = pipeline.parse_archive(staged_archive)
        if len(staged_rows) != len(rows):
            raise ValueError("Staged archive row count does not match audited dataset")
        pipeline.atomic_replace_group([
            (pipeline.ARCHIVE_PATH, staged_archive),
            (pipeline.DATA_JSON_PATH, staged_data),
            (pipeline.HOST_METADATA_PATH, staged_manifest),
            (pipeline.README_PATH, staged_readme),
            (pipeline.STATS_PATH, staged_stats),
        ])
        pipeline.sync_to_obsidian(dry=False)
    finally:
        try:
            if stage_dir is not None:
                pipeline._remove_directory_verified(stage_dir, root=pipeline.REPO_DIR.parent)
        finally:
            lock_pipeline.release_pipeline_lock()
    return stats


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Audit and apply Nadgryzieni host metadata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit_parser = subparsers.add_parser("audit", help="fetch and audit current records")
    audit_parser.add_argument("--output", required=True, type=Path)
    audit_parser.add_argument("--rate-limit", type=float, default=0.25)
    audit_parser.add_argument("--cache", type=Path, default=DEFAULT_HOST_CACHE_PATH)
    audit_parser.add_argument("--refresh", action="store_true", help="ignore the external result cache")
    apply_parser = subparsers.add_parser("apply", help="apply a matching audit")
    apply_parser.add_argument("--audit", required=True, type=Path)
    mode = apply_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.command == "audit":
        report = audit_repository(
            args.output,
            rate_limit=args.rate_limit,
            cache_path=args.cache,
            refresh=args.refresh,
        )
        print(json.dumps({
            "record_count": report["record_count"],
            "fetch_target_count": report["fetch_target_count"],
            "network_fetch_count": report["network_fetch_count"],
            "cache_hit_count": report["cache_hit_count"],
            "verified": sum(1 for entry in report["records"].values() if entry["hosts_status"] == "verified"),
            "not_listed": sum(1 for entry in report["records"].values() if entry["hosts_status"] == "not_listed"),
            "unresolved": sum(1 for entry in report["records"].values() if entry["hosts_status"] in UNRESOLVED_STATUSES),
        }, ensure_ascii=False))
        return 0
    stats = apply_audit(args.audit, dry_run=args.dry_run, write=args.write)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "FetchResult",
    "HostNameError",
    "HostParseResult",
    "ResponseValidationResult",
    "apply_audit",
    "audit_repository",
    "canonical_url",
    "create_ssl_context",
    "dataset_fingerprint",
    "decode_response_body",
    "fetch_https_html",
    "host_dedupe_key",
    "normalize_host_name",
    "parse_patreon_post_payload",
    "parse_result_to_dict",
    "parse_rrn_hosts",
    "record_key",
    "retry_after_seconds",
    "serialize_parse_result",
    "validate_response",
]


if __name__ == "__main__":
    raise SystemExit(_cli())
