"""Find skills related to a task, repair diagnosis, or maintenance goal.

The task query and component descriptions are embedded together. Retrieval keeps
the highest-scoring components and any components already chosen by the reuse
plan, returning their records, omitted IDs, and similarity scores.
"""

import json


def retrieve(controller, query):
    """Rank candidate descriptions and include the reuse plan's chosen components."""
    state = controller.state
    components = state.candidate
    names = sorted(components)
    if not names:
        return {'selected': {}, 'omitted': [], 'scores': {}}
    texts = [json.dumps({'query': query}, sort_keys=True)] + [
        json.dumps(
            {k: components[n].get(k) for k in ('id', 'interface', 'contract')}, sort_keys=True
        )
        for n in names
    ]
    # vectors: list[list[float]]; the first vector is the query, followed by component vectors.
    vectors = controller.embed(texts)
    q = vectors[0]
    # ranked: list[tuple[str, float]], pairing each component ID with its similarity score.
    ranked = sorted(
        zip(names, [sum((a * b for a, b in zip(q, v))) for v in vectors[1:]]),
        key=lambda x: (-x[1], x[0]),
    )
    # Keep the plan's chosen components even when they fall outside the top-k ranking.
    explicit = {
        v.get('component_id') for v in (state.reuse_plan or {}).get('choices', {}).values()
    }
    selected = {n for n, _ in ranked[: controller.top_k]} | explicit
    return {
        'selected': {n: components[n] for n in names if n in selected},
        'omitted': [n for n in names if n not in selected],
        'scores': dict(ranked),
    }
