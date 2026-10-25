"""协作流程：从成员中选择协调者，自动设定角色、分工和汇总。"""

import asyncio
import json
from copy import deepcopy
from datetime import datetime

from openai_codex import AsyncCodex, CodexConfig

from model_runtime import PROJECT_DIR, RUNTIME_OVERRIDES, configure_agents, create_agents
from shared_memory import create_memory
from file_tools import output_dir_for


PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "assignments": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "agent": {"type": "integer", "minimum": 1},
                    "role": {"type": "string"},
                    "instruction": {"type": "string"},
                },
                "required": ["agent", "role", "instruction"],
            },
        },
    },
    "required": ["summary", "assignments"],
}


# 追加一条本地日志；保存模型输入、回答、错误和 SDK 返回的用量信息。
def write_log(path, entry):
    entry = {"time": datetime.now().astimezone().isoformat(), **entry}
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


# 调用一个已有会话；只做一次尝试，规划时额外要求结构化 JSON 输出。
async def ask(agent, number, phase, instruction, memory_text, log_path, output_schema=None):
    prompt = json.dumps({"agent": number, "instruction": instruction, "shared_memory": memory_text}, ensure_ascii=False)
    entry = {"agent": number, "model": agent["model"], "phase": phase,
             "thread_id": getattr(agent["thread"], "id", None)}
    write_log(log_path, {**entry, "event": "started", "input": prompt})
    print(f"\n[成员 {number} / {phase}] 正在处理……", flush=True)
    options = {"effort": agent["effort"]}
    if output_schema is not None:
        options["output_schema"] = output_schema
    try:
        result = await agent["thread"].run(prompt, **options)
        # 只记录工具操作，不保存模型的内部推理内容。
        tool_calls = []
        for item in result.items:
            item = getattr(item, "root", item)
            if getattr(item, "type", None) in {"mcpToolCall", "collabAgentToolCall", "dynamicToolCall"}:
                tool_calls.append(item.model_dump(mode="json"))
        entry.update(turn_id=result.id, status=result.status.value, output=result.final_response, tool_calls=tool_calls,
                     usage=result.usage.model_dump(mode="json") if result.usage else None)
        if result.status.value != "completed":
            raise RuntimeError(f"本轮未完成：{result.status.value}，{result.error}")
        if not result.final_response or not result.final_response.strip():
            raise RuntimeError("模型没有返回有效回答。")
    except Exception as error:
        write_log(log_path, {**entry, "event": "failed", "error": str(error)})
        raise
    write_log(log_path, {**entry, "event": "finished"})
    if phase != "plan":
        print(f"\n[成员 {number} / {phase}]\n{result.final_response}", flush=True)
    return result.final_response


# 核对全员分工，确保每名成员（含协调者）恰好有一项非空任务。
def parse_plan(text, count):
    plan = json.loads(text)
    if not isinstance(plan, dict) or set(plan) != {"summary", "assignments"}:
        raise ValueError("分工必须包含 summary 和 assignments。")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise ValueError("分工说明不能为空。")
    assignments = plan["assignments"]
    if not isinstance(assignments, list) or len(assignments) != count:
        raise ValueError("必须为每名成员（包括协调者）各分配一项任务。")
    assigned = set()
    for item in assignments:
        if not isinstance(item, dict) or set(item) != {"agent", "role", "instruction"}:
            raise ValueError("每项分工必须包含 agent、role 和 instruction。")
        number = item["agent"]
        if type(number) is not int or not 1 <= number <= count or number in assigned:
            raise ValueError("执行成员编号无效或重复；每名成员必须恰好出现一次。")
        if not isinstance(item["role"], str) or not item["role"].strip():
            raise ValueError("成员角色不能为空。")
        if not isinstance(item["instruction"], str) or not item["instruction"].strip():
            raise ValueError("成员任务不能为空。")
        assigned.add(number)
    return plan


# 分工后全员（含协调者）并行执行；全部结束后，协调者再审查汇总。
async def cooperate(agents, task, memory_path, coordinator=1):
    if not agents or type(coordinator) is not int or not 1 <= coordinator <= len(agents):
        raise ValueError("至少需要一名成员，协调者编号必须在成员范围内。")
    log_path = memory_path.parent / "messages.jsonl"
    print(f"共享笔记：{memory_path}\n消息日志：{log_path}", flush=True)
    print(f"生成文件目录：{output_dir_for(memory_path)}", flush=True)
    if len(agents) == 1:
        print("预计启动 1 个用户回合：单个成员直接完成任务；工具往返可能增加模型推理与 token，无自动重试。", flush=True)
        return await ask(agents[0], 1, "work", {"phase": "work", "task": task, "role": "独立完成任务", "assignment": task},
                         memory_path.read_text(encoding="utf-8"), log_path)
    print(f"预计最多启动 {len(agents) + 2} 个用户回合：协调者分工 1 次、全员执行 {len(agents)} 次、协调者汇总 1 次。", flush=True)
    print("工具往返可能增加底层模型推理与 token；无自动重试，不自动开启下一轮。", flush=True)
    leader = agents[coordinator - 1]
    team = [{"agent": number, "model": agent["model"]}
            for number, agent in enumerate(agents, start=1)]
    schema = deepcopy(PLAN_SCHEMA)
    schema["properties"]["assignments"].update(minItems=len(agents), maxItems=len(agents))
    schema["properties"]["assignments"]["items"]["properties"]["agent"]["maximum"] = len(agents)
    plan_text = await ask(leader, coordinator, "plan", {"phase": "plan", "task": task, "members": team},
                          memory_path.read_text(encoding="utf-8"), log_path, schema)
    plan = parse_plan(plan_text, len(agents))
    print(f"\n[分工] {plan['summary']}", flush=True)
    for item in plan["assignments"]:
        agents[item["agent"] - 1]["role"] = item["role"]
        print(f"成员 {item['agent']} / {item['role']}：{item['instruction']}", flush=True)

    memory_text = memory_path.read_text(encoding="utf-8")
    answers = await asyncio.gather(*(
        ask(agents[item["agent"] - 1], item["agent"], "work",
            {"phase": "work", "task": task, "role": item["role"], "plan_summary": plan["summary"], "assignment": item["instruction"]},
            memory_text, log_path)
        for item in plan["assignments"]
    ), return_exceptions=True)
    results = []
    for item, answer in zip(plan["assignments"], answers):
        failed = isinstance(answer, BaseException)
        results.append({"agent": item["agent"], "ok": not failed,
                        "error" if failed else "answer": str(answer)})
        if failed:
            print(f"成员 {item['agent']} 失败：{answer}；保留其他结果，不重试。", flush=True)

    return await ask(leader, coordinator, "review", {"phase": "review", "task": task, "plan": plan, "results": results},
                     memory_path.read_text(encoding="utf-8"), log_path)


# 全队共用一次选择的模型；仅被选为协调者的成员加载 leader.md。
async def run_team(task, count, coordinator=1):
    if type(count) is not int or count < 1 or type(coordinator) is not int or not 1 <= coordinator <= count:
        raise ValueError("人数或协调者编号无效。")
    instructions = (PROJECT_DIR / "agent.md").read_text(encoding="utf-8")
    leader_instructions = (PROJECT_DIR / "leader.md").read_text(encoding="utf-8")
    if not instructions.strip() or not leader_instructions.strip():
        raise ValueError("agent.md 和 leader.md 不能为空。")
    config = CodexConfig(cwd=str(PROJECT_DIR), config_overrides=RUNTIME_OVERRIDES)
    async with AsyncCodex(config=config) as codex:
        configurations = await configure_agents(codex, count)
        if not configurations:
            return
        if count > 1:
            configurations[coordinator - 1]["role"] = leader_instructions
        memory_path = create_memory(PROJECT_DIR)
        agents = await create_agents(codex, configurations, instructions, memory_path)
        await cooperate(agents, task, memory_path, coordinator)


# 接收人数，自动指定成员 1 为协调者，输入任务后启动一次协作。
def run_collaboration(count):
    print("全队共用一个模型；执行角色与具体分工由协调者根据任务自动设置。")
    if count > 1:
        print("已自动指定成员 1 为协调者。")
    task = ""
    while not task:
        task = input("请输入共同任务（exit 退出）：").strip()
        if task.lower() == "exit":
            return
    asyncio.run(run_team(task, count))


if __name__ == "__main__":
    print("这是协作运行模块。启动程序请运行：python main.py")
