"""负责共用模型选择、独立会话创建与工具绑定。"""

import sys
from pathlib import Path

from openai_codex import ApprovalMode, Sandbox
from file_tools import FILE_TOOL_NAMES, output_dir_for


PROJECT_DIR = Path(__file__).resolve().parent
# 在启动运行时就关闭内置派生智能体和自动续跑；人数与阶段只由 Python 控制。
RUNTIME_OVERRIDES = (
    'forced_login_method="chatgpt"',
    'agents.enabled=false',
    'features.multi_agent=false',
    'features.goals=false',
)


# 读取菜单编号；无效输入重新询问，exit 返回 None 表示退出。
def choose_number(prompt, maximum):
    while True:
        text = input(prompt).strip()
        if text.lower() == "exit":
            return None
        try:
            number = int(text)
            if 1 <= number <= maximum:
                return number
        except ValueError:
            pass
        print(f"请输入 1 到 {maximum} 的编号，或输入 exit 退出。")


# 模型只选一次，所有成员共用；执行角色留空，之后由协调者自动分配。
async def configure_agents(codex, count):
    catalog = await codex.models()
    if not catalog.data:
        raise RuntimeError("Codex 没有返回可选择的模型。")
    print("\nCodex 返回的模型目录（实际调用仍受账号权限与额度限制）：")
    for index, model in enumerate(catalog.data, start=1):
        print(f"{index}. {model.display_name} ({model.model})")
    number = choose_number("为所有成员选择一个模型编号：", len(catalog.data))
    if number is None:
        return []
    return [{"model": catalog.data[number - 1], "role": ""} for _ in range(count)]


# 按配置创建独立会话；每个角色只交给对应会话，通用要求来自 agent.md。
async def create_agents(codex, configurations, instructions, memory_path):
    agents = []
    for number, configuration in enumerate(configurations, start=1):
        model = configuration["model"]
        role = configuration["role"]
        agent_instructions = instructions + ("\n\n本会话的角色要求：\n" + role if role else "")
        thread = await codex.thread_start(
            model=model.model,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            config={
                "features.multi_agent": False,
                "agents.enabled": False,
                "mcp_servers.shared_notes": {
                    "command": sys.executable,
                    "args": [str(PROJECT_DIR / "memory_tool.py"), str(memory_path), str(number),
                             "--output-dir", str(output_dir_for(memory_path))],
                    "enabled_tools": ["update_note", *FILE_TOOL_NAMES],
                    "required": True,
                },
            },
            developer_instructions=agent_instructions,
        )
        efforts = [option.reasoning_effort.value for option in model.supported_reasoning_efforts]
        effort = "low" if "low" in efforts else model.default_reasoning_effort.value
        agents.append({"thread": thread, "model": model.model, "effort": effort, "role": role})
    return agents


