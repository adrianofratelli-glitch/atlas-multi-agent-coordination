from collections import defaultdict
from typing import Hashable

KB_VECTOR_INDEX = "kb_autoembed_v1"
KB_LEXICAL_INDEX = "kb_lexical_v1"
KB_PROJECT_FIELDS = ("article_id", "title", "content", "category")


def reciprocal_rank_fusion(
    rankings: list[list[dict]], *, key: str = "_id", k: int = 60, limit: int = 5
) -> list[dict]:
    """Combina rankings lexical e vetorial sem comparar escalas de score.

    Continua sendo o caminho de DEMO_MODE/CI (ranking local, sem Atlas). No Atlas real a
    fusão roda server-side em build_kb_rank_fusion_pipeline — mesma matemática, uma ida ao banco.
    """
    scores: dict[Hashable, float] = defaultdict(float)
    documents: dict[Hashable, dict] = {}
    for ranking in rankings:
        for position, document in enumerate(ranking, start=1):
            document_key = document[key]
            documents[document_key] = document
            scores[document_key] += 1 / (k + position)
    ordered = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [{**documents[item], "rrf_score": round(scores[item], 6)} for item in ordered]


def build_kb_vector_pipeline(query: str, *, limit: int = 10) -> list[dict]:
    """Perna semântica: Atlas Vector Search com Automated Embedding (voyage-4)."""
    return [
        {"$vectorSearch": {
            "index": KB_VECTOR_INDEX,
            "path": "content",
            "query": {"text": query},
            "model": "voyage-4",
            "numCandidates": max(50, limit * 5),
            "limit": limit,
        }},
        {"$project": {field: 1 for field in KB_PROJECT_FIELDS} | {"_id": 0}},
    ]


def build_kb_lexical_pipeline(query: str, *, limit: int = 10) -> list[dict]:
    """Perna lexical: BM25 com boost no título (analyzer português)."""
    return [
        {"$search": {"index": KB_LEXICAL_INDEX, "compound": {"should": [
            {"text": {"query": query, "path": "title", "score": {"boost": {"value": 2}}}},
            {"text": {"query": query, "path": "content"}},
        ], "minimumShouldMatch": 1}}},
        {"$limit": limit},
        {"$project": {field: 1 for field in KB_PROJECT_FIELDS} | {"_id": 0}},
    ]


def build_kb_rank_fusion_pipeline(query: str, *, limit: int = 4) -> list[dict]:
    """Híbrido server-side: $rankFusion funde as duas pernas dentro do banco (MongoDB 8.1+).

    Substitui o RRF na aplicação: uma agregação em vez de dois round-trips + fusão em Python.
    A matemática é a mesma (reciprocal rank), mas o ranking sai do servidor já ordenado — e o
    ranking intermediário (até 40 docs por perna) nunca trafega pela rede.
    """
    leg_limit = max(limit * 4, 20)
    return [
        {"$rankFusion": {"input": {"pipelines": {
            "vector": build_kb_vector_pipeline(query, limit=leg_limit)[:1],
            "lexical": build_kb_lexical_pipeline(query, limit=leg_limit)[:2],
        }}}},
        {"$limit": limit},
        {"$project": {field: 1 for field in KB_PROJECT_FIELDS}
         | {"_id": 0, "rrf_score": {"$meta": "score"}}},
    ]
