from datetime import date, timedelta

from app.agent.prompts import PYTHON_BASH_COMMAND, SYSTEM_PROMPT
from app.agent.tools.bash import create_bash_tool
from test.regression_support import run_tool, text

# 计划提示口径必须与实际契约、真实工具参数校验一致；这里逐条核对可行动约束，不做语义判定。
PLAN_REQUIRED = [
    '{"base_profile_version":1,"base_plan_id":null}',
    '["/repeat","/days/0/exercises/0/sets","/days/0/notes","/notes"]',
    "business_kind=plan",
    "禁止字符串\"None\"、\"null\"、空字符串、数字0或省略",
    "键必须存在",
    "每个训练日有具体动作，每个动作sets为正整数，reps为明确正整数或duration_seconds为正数，两者至少一个有值；休息日exercises=[]",
    "有重量（包括0）必须明确load_convention",
    "rest_seconds允许null",
    "目录外动作exercise_id=null",
    "以可信business_context.business_date为截止日",
    "逐页读取所需记录直至覆盖total",
    "同会话新快照成功使旧pending失效",
    "禁止自动保存、将修改式确认授权旧内容",
    "get_plan_save_status核对原操作或沿用原绑定幂等save_plan",
    "重复确认保持原ID内容时间",
    "一条用户确认节点跨画像/训练/计划只授权指定一份快照",
    "禁止混用其他业务绑定或编造ID",
    "历史与实际训练记录保持完整",
    "出现明确医疗风险停止相关建议并提示就医",
    "禁止在工具参数中传入 session_id、run_id、request_entry_id、source_entry_id 等身份字段",
]


def check():
    assert PYTHON_BASH_COMMAND in SYSTEM_PROMPT
    assert "__PYTHON_BASH_COMMAND__" not in SYSTEM_PROMPT
    assert '{"base_profile_version":1,"base_plan_id":null}' in SYSTEM_PROMPT
    assert '["/repeat","/days/0/exercises/0/sets","/days/0/notes","/notes"]' in SYSTEM_PROMPT
    assert "business_kind=plan" in SYSTEM_PROMPT
    assert "禁止探测解释器" in SYSTEM_PROMPT
    missing = [item for item in PLAN_REQUIRED if item not in SYSTEM_PROMPT]
    assert not missing, missing
    day = "2026-01-01"
    command = (f"{PYTHON_BASH_COMMAND} -c 'from datetime import date,timedelta; "
               f'print((date.fromisoformat("{day}")-timedelta(days=6)).isoformat())' + "'")
    message = run_tool(create_bash_tool(), {"command": command})
    assert not message.is_error
    assert text(message).strip() == (date.fromisoformat(day) - timedelta(days=6)).isoformat()
    print("PASS: actual system prompt trusted Python command, JSON null literal, JSON Pointer, plan tool constraints "
          f"({len(PLAN_REQUIRED)} clauses), real Bash 7-day date calculation")


if __name__ == "__main__":
    check()
