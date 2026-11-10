"""Rewrite：按方案修改已有组件的源码、接口与契约。

输入包含目标版本、相关分析、诊断与整份协调方案；交付由 CodeGen 的
apply_component 统一应用，后续进入 Validate。
"""

from .codegen import edit_context


def rewrite(controller, edit, public):
    """Request a replacement for the proposal's exact existing source version."""
    component = controller.state.candidate[edit['target']]
    if component['hash'] != edit['expected_hash']:
        raise ValueError('Stale rewrite source')
    return controller.operator('Rewrite', edit_context(controller, edit, public))


INSTRUCTIONS = 'Return target, expected_hash, source, interface, contract. Apply assigned coordinated migration with preserved behavior. Retain unrelated edits.'
