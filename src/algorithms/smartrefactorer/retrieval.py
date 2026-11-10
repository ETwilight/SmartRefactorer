"""Retrieve：根据当前任务、维修诊断或维护机会检索候选技能版本。

检索按描述 embedding 排序，保留 top_k 与复用计划明确选中的组件，
同时返回未选组件及全部相似度分数。
"""

import json


def retrieve(controller, query):
    """Rank candidate skill descriptions and retain explicitly chosen versions."""
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
    vectors = controller.embed(texts)
    q = vectors[0]
    ranked = sorted(
        zip(names, [sum((a * b for a, b in zip(q, v))) for v in vectors[1:]]),
        key=lambda x: (-x[1], x[0]),
    )
    # 复用计划已经选定的组件与相似度 top_k 取并集。
    explicit = {
        v.get('component_id') for v in (state.reuse_plan or {}).get('choices', {}).values()
    }
    selected = {n for n, _ in ranked[: controller.top_k]} | explicit
    return {
        'selected': {n: components[n] for n in names if n in selected},
        'omitted': [n for n in names if n not in selected],
        'scores': dict(ranked),
    }
