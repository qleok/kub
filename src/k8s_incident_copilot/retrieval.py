"""Offline retrieval and document-backed enrichment for classified incidents."""

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from math import fsum, log
from typing import cast, get_args

from pydantic import AnyHttpUrl

from .schemas import DiagnosisReport, FailureType, IncidentInput, RetrievedReference

_WORD = re.compile(r"[a-z0-9]+")
_SECTION = re.compile(r"^## +", flags=re.MULTILINE)
_STOPWORDS = frozenset(
    "a an and are as at be by for from in is it of on or that the to was with".split()
)
_FAILURE_TYPES = frozenset(get_args(FailureType))
_MAX_CITATIONS = 2


@dataclass(frozen=True)
class Passage:
    failure_type: FailureType
    reference: RetrievedReference
    terms: frozenset[str]


@dataclass(frozen=True)
class KnowledgeIndex:
    passages: tuple[Passage, ...]
    weights: dict[str, float]


def _terms(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(text.casefold())) - _STOPWORDS


def _split_document(document_id: str, markdown: str) -> list[Passage]:
    header, *sections = _SECTION.split(markdown.strip())
    failure_type = header.strip().removeprefix("# ")
    if not header.startswith("# ") or failure_type not in _FAILURE_TYPES or not sections:
        raise ValueError(f"Invalid knowledge document header: {document_id}")

    passages = []
    for section in sections:
        heading, _, body = section.partition("\n")
        source, _, body = body.strip().partition("\n")
        excerpt, separator, action = body.strip().partition("\nAction: ")
        slug = "-".join(_WORD.findall(heading.casefold()))
        if not (
            slug
            and source.startswith("Source: ")
            and excerpt.strip()
            and separator
            and action.strip()
        ):
            raise ValueError(f"Malformed knowledge section: {document_id}/{slug}")

        url = AnyHttpUrl(source.removeprefix("Source: ").strip())
        if not (
            url.scheme == "https"
            and url.host == "kubernetes.io"
            and (url.path or "").startswith("/docs/")
            and url.fragment
        ):
            raise ValueError(f"Expected a Kubernetes documentation section: {document_id}/{slug}")

        reference = RetrievedReference(
            title=heading.strip(),
            url=url,
            chunk_id=f"{document_id}/{slug}",
            excerpt=excerpt.strip(),
            recommended_step=action.strip(),
        )
        passages.append(
            Passage(
                failure_type=cast(FailureType, failure_type),
                reference=reference,
                terms=_terms(f"{heading} {excerpt} {action}"),
            )
        )
    return passages


@lru_cache(maxsize=1)
def _load_index() -> KnowledgeIndex:
    corpus = files("k8s_incident_copilot").joinpath("knowledge")
    passages = tuple(
        passage
        for resource in corpus.iterdir()
        if resource.is_file() and resource.name.endswith(".md")
        for passage in _split_document(
            resource.name.removesuffix(".md"), resource.read_text("utf-8")
        )
    )
    ids = {passage.reference.chunk_id for passage in passages}
    if (
        len(ids) != len(passages)
        or {passage.failure_type for passage in passages} != _FAILURE_TYPES
    ):
        raise ValueError("Knowledge base must cover all supported failures with unique chunk IDs.")

    frequencies = Counter(term for passage in passages for term in passage.terms)
    weights = {
        term: 1 + log((len(passages) + 1) / (count + 1)) for term, count in frequencies.items()
    }
    return KnowledgeIndex(passages, weights)


def _retrieve(incident: IncidentInput, failure_type: FailureType) -> list[RetrievedReference]:
    # The query stays in memory and is never copied into report citations.
    observations = [
        incident.status or "",
        incident.container_reason or "",
        incident.description or "",
        *incident.logs,
    ]
    for event in incident.events:
        observations.extend((event.reason or "", event.message or ""))

    label = failure_type.casefold()
    query = _terms(" ".join(observations)) - {label}
    index = _load_index()
    matches: list[tuple[float, RetrievedReference]] = []
    for passage in index.passages:
        if passage.failure_type != failure_type:
            continue
        overlap = query & passage.terms
        # Keep the general failure explanation eligible without boosting it over specific clues.
        if overlap or label in passage.terms:
            score = fsum(index.weights[term] for term in overlap)
            matches.append((score, passage.reference))
    matches.sort(key=lambda item: (-item[0], item[1].chunk_id))
    return [reference for _, reference in matches[:_MAX_CITATIONS]]


def augment(incident: IncidentInput, report: DiagnosisReport) -> DiagnosisReport:
    """Add retrieved recommendations without changing the rule diagnosis."""
    if report.failure_type is None:
        return report

    references = _retrieve(incident, report.failure_type)
    if not references:
        return report

    steps = dict.fromkeys(
        [*report.recommended_steps, *(ref.recommended_step for ref in references)]
    )
    return report.model_copy(
        update={
            "references": references,
            "recommended_steps": list(steps),
            "diagnosis_method": "rules+rag",
        }
    )
