from app.retrieval import (KB_LEXICAL_INDEX, KB_VECTOR_INDEX, build_kb_rank_fusion_pipeline,
                           reciprocal_rank_fusion)


def test_rrf_rewards_documents_present_in_both_rankings():
    vector = [{"_id": "a"}, {"_id": "b"}, {"_id": "c"}]
    lexical = [{"_id": "b"}, {"_id": "d"}, {"_id": "a"}]
    result = reciprocal_rank_fusion([vector, lexical])
    assert result[0]["_id"] == "b"
    assert {item["_id"] for item in result} == {"a", "b", "c", "d"}



def test_rank_fusion_pipeline_fuses_both_legs_server_side():
    pipeline = build_kb_rank_fusion_pipeline("produto com defeito", limit=4)
    legs = pipeline[0]["$rankFusion"]["input"]["pipelines"]
    assert set(legs) == {"vector", "lexical"}
    # A perna vetorial entra sem $project: quem projeta é o estágio final, depois da fusão.
    assert list(legs["vector"][0]) == ["$vectorSearch"]
    assert legs["vector"][0]["$vectorSearch"]["index"] == KB_VECTOR_INDEX
    assert legs["lexical"][0]["$search"]["index"] == KB_LEXICAL_INDEX
    # Cada perna ranqueia mais fundo que o limite final para a fusão ter o que reordenar.
    assert legs["vector"][0]["$vectorSearch"]["limit"] >= 20
    assert pipeline[-2] == {"$limit": 4}
    assert pipeline[-1]["$project"]["rrf_score"] == {"$meta": "score"}
