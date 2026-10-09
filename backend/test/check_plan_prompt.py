import json
from datetime import date, timedelta
from uuid import uuid4

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.business import business_tool_declarations
from app.agent.tools.dates import bind_date_tools, date_tool_declarations
from app.agent.tools.plan_import import plan_import_tool_declarations
from app.domain.business.models import BusinessContext
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
    "用calculate_date传days_offset=-6得到起始日期",
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
    "用户提供自己编写或他人给出的现有计划并要求录入或保存该计划时使用prepare_plan_import",
    "用户要求调整本次提供的计划内容或已保存的当前计划时使用prepare_plan_adjustment",
    "prepare_plan仅用于用户要求生成新的计划建议，用户提供既有计划内容时禁止改用prepare_plan",
    "不含preparation_kind，准备类型由后端按实际工具确定",
    '禁止字符串"null"、空字符串、数字0或省略；参数预处理仅将两依据字段精确字符串"None"转为null，其他字段和值原样严格校验',
    '无画像且无当前计划的基础字段准确示例：{"base_profile_version":null,"base_plan_id":null}',
    "无画像时base_profile_version写JSON null；已有画像时写get_profile返回的真实版本，禁止用null跳过依据及限制检查",
    "未知动作详情用exercises=[]",
    "理由与依据写入整体notes和训练日notes完整展示",
    "准备前先get_profile读取真实画像状态与版本、get_current_plan读取真实当前计划ID",
    "完整展示之后等待后续用户明确确认",
    "按修改处理，重新准备并完整展示新快照，等待再次确认",
]

DATE_PROMPT = [
    "明确相对时间使用已注册 calculate_date 计算，days_offset 为严格整数：今天0、昨天-1、N天前-N",
    "基准由后端绑定为可信 business_context.business_date",
    "用calculate_date传days_offset=-6得到起始日期",
]

# 调整专用提示段：逐字核对固定取数参数、可信日期、内容保留、来源路径同步、
# notes 改动依据、训练事实时效及重新确认规则；均为提示词声明验证，不涉及模型行为判定。
ADJUSTMENT_PROMPT = [
    "调整前固定查询最近10条完整实际训练记录：list_workouts(date_from=null, date_to=business_context.business_date, page=1, page_size=10)",
    "business_date使用后端可信运行上下文；读取返回的全部items，记录总数为x时使用min(x, 10)条，0条时为空数组[]",
    "每条是一个训练日期的完整最新记录，按performed_on降序、id降序，含实际动作、逐组表现、重量口径和训练notes",
    "prepare_plan_adjustment提交全部声明字段的完整最终payload，保留未涉及字段及原有suggested_fields建议来源",
    "训练日或动作数组位置变化时同步来源路径",
    "修改位置、原值、新值、理由、实际训练依据（含所用记录日期、相关逐组表现及必要重量口径）、依据不足或核实状态、必要注意事项写入计划或训练日notes，删除内容的说明同样写入notes",
    "调整提案固定使用准备时查询到的训练事实。确认前新增训练或修正记录，原提案内容及状态保持原值",
    "用户要求采用新增或修正事实时重新查询、整理完整最终payload、调用prepare_plan_adjustment重新准备，完整展示新提案及所用依据，等待后续用户再次明确确认",
    "需要补充或更新画像时先走画像准备、完整展示、后续确认及保存流程，取得真实新版本后再准备计划",
]

# 调整工具声明与提示段同源：模型可见声明必须逐条承载相同的固定参数与规则。
ADJUSTMENT_DECLARATION = [
    "固定查询list_workouts(date_from=null, date_to=business_context.business_date, page=1, page_size=10)",
    "business_date来自后端可信运行上下文，读取返回的全部items；总数为x时使用min(x, 10)条，",
    "0条为空数组[]。每条为一个训练日期的完整最新记录，按performed_on降序、id降序，",
    "包含实际动作、逐组表现、重量口径及训练notes，使用其中与本次任务相关的表现。",
    "全部声明字段均须提交，保留未涉及字段、原有说明及suggested_fields建议来源；",
    "suggested_fields按最终内容用JSON Pointer标记助手补充或修改的建议，数组位置变化时同步来源路径。",
    "计划或训练日notes写明修改位置、原值、新值、理由、实际训练依据、记录日期、必要重量口径、",
    "提案固定使用准备时查询到的训练事实，新增或修正训练记录保持原提案内容及状态；",
    "用户要求采用新事实时重新查询、准备完整提案、展示全部业务字段、notes、建议来源及所用依据，",
    "需要补充或更新画像时完成画像展示、后续确认及保存，取得真实新版本。",
]

# 提示段与声明共用的字面串：固定参数、min(x, 10)、倒序口径两处必须完全一致。
ADJUSTMENT_SHARED = [
    "list_workouts(date_from=null, date_to=business_context.business_date, page=1, page_size=10)",
    "min(x, 10)",
    "按performed_on降序、id降序",
]

# 生成流程的既有取数口径必须保持原值，与被调整段并列存在且互不替换。
GENERATION_RECENT = [
    "生成前读取近7自然日真实训练：以可信business_context.business_date为截止日",
    "逐页读取所需记录直至覆盖total",
]

# 工具声明即契约：模型可见的日期口径必须与注册声明逐条一致。
DATE_DECLARATION = [
    "days_offset 为必填严格整数",
    "0 今天、-1 昨天、-N 为 N 天前",
    "近七个自然日的起始偏移为 -6",
    '结果为一个 JSON 文本：{"date":"YYYY-MM-DD"}',
    "禁止传入基准日期、时区或身份字段",
]


def context_for(business_date: str) -> BusinessContext:
    return BusinessContext(
        timezone="Asia/Shanghai",
        business_date=business_date,
        session_id=str(uuid4()),
        run_id=str(uuid4()),
        request_entry_id=str(uuid4()),
        source_entry_id=str(uuid4()),
    )


def check():
    assert "__PYTHON_BASH_COMMAND__" not in SYSTEM_PROMPT
    assert "bash" not in SYSTEM_PROMPT and "解释器" not in SYSTEM_PROMPT
    assert '{"base_profile_version":1,"base_plan_id":null}' in SYSTEM_PROMPT
    assert '["/repeat","/days/0/exercises/0/sets","/days/0/notes","/notes"]' in SYSTEM_PROMPT
    assert "business_kind=plan" in SYSTEM_PROMPT
    missing = [item for item in PLAN_REQUIRED + DATE_PROMPT if item not in SYSTEM_PROMPT]
    assert not missing, missing

    # 调整专用提示段逐条落地，缺失即声明与契约脱钩。
    prompt_missing = [item for item in ADJUSTMENT_PROMPT if item not in SYSTEM_PROMPT]
    assert not prompt_missing, prompt_missing
    generation_missing = [item for item in GENERATION_RECENT if item not in SYSTEM_PROMPT]
    assert not generation_missing, generation_missing

    # 生产注册表与模型可见声明同源，二者 prepare_plan_adjustment 描述必须逐字一致。
    production = {item.name: item for item in business_tool_declarations()}
    declared = {item.name: item for item in plan_import_tool_declarations()}
    assert production["prepare_plan_adjustment"].description == declared[
        "prepare_plan_adjustment"
    ].description
    adjustment_description = production["prepare_plan_adjustment"].description
    declaration_missing = [item for item in ADJUSTMENT_DECLARATION if item not in adjustment_description]
    assert not declaration_missing, declaration_missing

    # 固定参数、min(x, 10) 与倒序口径在提示段和声明两处保持同一字面，声明即契约。
    shared_missing = [
        item for item in ADJUSTMENT_SHARED
        if item not in adjustment_description or item not in SYSTEM_PROMPT
    ]
    assert not shared_missing, shared_missing

    # 日期口径与生产注册表内的真实声明同源，声明变化即测试失败。
    description = production["calculate_date"].description
    assert description == date_tool_declarations()[0].description
    absent = [item for item in DATE_DECLARATION if item not in description]
    assert not absent, absent

    # 提示口径的七个自然日范围由真实工具的严格契约产生，基准只来自绑定的可信上下文。
    day = "2026-01-01"
    message = run_tool(bind_date_tools(context_for(day))["calculate_date"], {"days_offset": -6})
    assert not message.is_error
    assert json.loads(text(message)) == {
        "date": (date.fromisoformat(day) - timedelta(days=6)).isoformat()
    }
    print("PASS: system prompt JSON null literal, JSON Pointer, plan tool constraints "
          f"({len(PLAN_REQUIRED)} clauses), adjustment prompt ({len(ADJUSTMENT_PROMPT)} clauses) "
          f"and declaration ({len(ADJUSTMENT_DECLARATION)} clauses) with shared literals "
          f"({len(ADJUSTMENT_SHARED)}), generation caliber ({len(GENERATION_RECENT)}), "
          f"calculate_date prompt contract ({len(DATE_PROMPT)} clauses) and declaration contract "
          f"({len(DATE_DECLARATION)} clauses) with real 7-day range")


if __name__ == "__main__":
    check()
