"""Baseline 策略：读取近期反馈和显式工作记忆，生成请求或结束当前轮次。

remember 用模型提交的文本替换工作记忆；模型、存储和时间戳由宿主注入。
"""

import copy
from pathlib import Path


class Policy:
    compact_transport = True

    def __init__(self, model, save, instructions, decision_schema, clock):
        self.model = model
        self.save = save
        self.clock = clock
        self.instructions = instructions
        self.decision_schema = decision_schema
        self.requests = []
        self.memory = ''

    def bind(self, folder, recorder):
        self.memory_file = Path(folder) / 'method/state/working-memory.json'

    def decide(self, context):
        # 决策窗口保留最近六次反馈，更早记录通过反馈读取接口获取。
        context = {
            **context,
            'history': context['history'][-6:],
            'working_memory': self.memory,
            'history_access': {
                'retained_decisions': 6,
                'original_decisions': context.get('history_offset', 0)
                + len(context['history']),
                'older_results': 'feedback.list/get',
            },
        }
        decision, meta = self.model(
            self.instructions,
            context,
            self.decision_schema,
            'baseline_decision',
            max_output_tokens=32768,
        )
        if decision['decision'] == 'remember':
            # remember 提交完整的新工作记忆，后续决策读取这份文本。
            self.memory = decision['report']
            if hasattr(self, 'memory_file'):
                self.save(
                    self.memory_file,
                    {
                        'source': 'agent_written',
                        'text': self.memory,
                        'updated_at': self.clock(),
                    },
                )
        self.requests.append(meta)
        return decision
