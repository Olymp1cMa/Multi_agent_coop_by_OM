"""用假会话验证分工、并发和失败路径，不启动模型推理。"""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import TurnResult
from openai_codex.generated.v2_all import ThreadItem, TurnStatus

import collaboration
import main
from shared_memory import create_memory, update_note


# 使用 SDK 真实结果类型，确保正确处理枚举状态与空用量。
def reply(text, status=TurnStatus.completed):
    return TurnResult(id="test", status=status, error=None, started_at=None,
                      completed_at=None, duration_ms=None, final_response=text, items=[], usage=None)


# 构造独立的假会话，实际 SDK 客户端不会被调用。
def agent(answers):
    return {"thread": SimpleNamespace(run=AsyncMock(side_effect=answers)),
            "model": "test-model", "effort": "low", "role": "测试角色"}


# 为不同测试生成分工，正式流程要求全员各有一项任务。
def plan(*numbers):
    return json.dumps({"summary": "处理当前可独立执行的部分", "assignments": [
        {"agent": number, "role": f"第{number}组核算员，关注重复订单", "instruction": f"任务{number}，交付小计并说明核算依据"}
        for number in numbers]}, ensure_ascii=False)


class PlanTests(unittest.TestCase):
    # 不信任模型输出，拦截越界、重复、空白和格式错误的分工。
    def test_validate_plan(self):
        self.assertEqual(len(collaboration.parse_plan(plan(1, 2, 3), 3)["assignments"]), 3)
        self.assertEqual(len(collaboration.parse_plan(plan(*range(1, 26)), 25)["assignments"]), 25)
        invalid = ["not json", "[]", plan(), plan(1), plan(2, 3), plan(1, 2, 4), plan(1, 2, 2)]
        for key, value in (("agent", True), ("role", " "), ("instruction", " "), ("role", None)):
            bad = json.loads(plan(1, 2, 3))
            bad["assignments"][1][key] = value
            invalid.append(json.dumps(bad))
        bad = json.loads(plan(1, 2, 3))
        del bad["assignments"][1]["role"]
        invalid.append(json.dumps(bad))
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                collaboration.parse_plan(text, 3)

    # 在启动界面退出时不打开客户端，也不产生模型调用。
    def test_exit_before_sdk(self):
        with patch("builtins.input", return_value="exit"), patch.object(collaboration, "run_team") as runner:
            with contextlib.redirect_stdout(io.StringIO()):
                collaboration.run_collaboration(3)
        runner.assert_not_called()

    # 从真正的 main 走完整交互；只输入人数、任务和模型，自动指定成员 1 统筹。
    def test_main_full_local_flow(self):
        entries = [agent([reply(plan(1, 2, 3)), reply("协调者执行成果"), reply("汇总完成")]), agent([reply("第一部分")]), agent([reply("第二部分")])]
        shared_model = SimpleNamespace(model="same-model", display_name="共用模型",
                                       supported_reasoning_efforts=[], default_reasoning_effort=SimpleNamespace(value="low"))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.models.return_value = SimpleNamespace(data=[shared_model])
        client.thread_start.side_effect = [entry["thread"] for entry in entries]
        with tempfile.TemporaryDirectory() as folder:
            memory = create_memory(Path(folder))
            with patch.object(collaboration, "AsyncCodex", return_value=client), \
                 patch.object(collaboration, "create_memory", return_value=memory), \
                 patch("builtins.input", side_effect=["3", "共同任务", "1"]) as inputs, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                main.main()
            self.assertEqual(inputs.call_count, 3)
            self.assertEqual(client.thread_start.await_count, 3)
            self.assertTrue(all(call.kwargs["model"] == "same-model" for call in client.thread_start.call_args_list))
            self.assertIn("汇总完成", output.getvalue())
            self.assertIn("第2组核算员", output.getvalue())
            self.assertIn("第3组核算员", output.getvalue())
            self.assertNotIn("设置角色", "".join(str(call) for call in inputs.call_args_list))
            self.assertNotIn("选择协调者", "".join(str(call) for call in inputs.call_args_list))
            self.assertIn("已自动指定成员 1 为协调者", output.getvalue())
            self.assertEqual(entries[0]["thread"].run.await_count, 3)


class LeaderTests(unittest.IsolatedAsyncioTestCase):
    # 每个测试使用临时笔记，退出测试时清理所有记录。
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.memory = create_memory(Path(folder.name))

    # 协调者与其他成员一起执行，所有回合共用原会话，阶段间刷新笔记。
    async def test_full_round_concurrency_and_fresh_notes(self):
        started, gate = set(), asyncio.Event()
        leader = agent([])
        workers = [agent([]), agent([])]

        # 三名成员全部进入才能放行，确保协调者不是先做完自己的任务再派工。
        async def work(prompt, **options):
            payload = json.loads(prompt)
            number = payload["agent"]
            started.add(number)
            update_note(self.memory, number, "", 0, f"成员{number}已验证小计")
            if len(started) == 3:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=1)
            return reply(f"结果{number}")

        for worker in workers:
            worker["thread"].run.side_effect = work

        # 同一个协调者会话依次处理规划、执行、审核，执行阶段才进入并发屏障。
        async def lead(prompt, **options):
            phase = json.loads(prompt)["instruction"]["phase"]
            if phase == "plan":
                return reply(plan(1, 2, 3))
            if phase == "review":
                return reply("总结果")
            return await work(prompt, **options)

        leader["thread"].run.side_effect = lead
        with contextlib.redirect_stdout(io.StringIO()):
            result = await collaboration.cooperate([leader, *workers], "求总和", self.memory)
        self.assertEqual(result, "总结果")
        self.assertEqual(started, {1, 2, 3})
        calls = leader["thread"].run.call_args_list
        self.assertIn("output_schema", calls[0].kwargs)
        self.assertNotIn("output_schema", calls[1].kwargs)
        self.assertNotIn("output_schema", calls[2].kwargs)
        self.assertEqual(calls[0].kwargs["output_schema"]["properties"]["assignments"]["minItems"], 3)
        self.assertEqual(calls[0].kwargs["output_schema"]["properties"]["assignments"]["maxItems"], 3)
        self.assertEqual([json.loads(call.args[0])["instruction"]["phase"] for call in calls], ["plan", "work", "review"])
        review = json.loads(calls[2].args[0])
        self.assertEqual([row["answer"] for row in review["instruction"]["results"]], ["结果1", "结果2", "结果3"])
        self.assertIn("成员1已验证小计", review["shared_memory"])
        self.assertIn("成员2已验证小计", review["shared_memory"])
        self.assertIn("成员3已验证小计", review["shared_memory"])
        snapshots = [json.loads(calls[1].args[0])["shared_memory"]] + [json.loads(worker["thread"].run.call_args.args[0])["shared_memory"] for worker in workers]
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))
        for number, worker in enumerate(workers, start=2):
            instruction = json.loads(worker["thread"].run.call_args.args[0])["instruction"]
            self.assertEqual(instruction["role"], f"第{number}组核算员，关注重复订单")
            self.assertEqual(worker["role"], instruction["role"])
        events = [json.loads(line) for line in (self.memory.parent / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(len(events), 10)
        self.assertEqual([event["phase"] for event in events if event["event"] == "started"], ["plan", "work", "work", "work", "review"])
        self.assertTrue(all(event["status"] == "completed" for event in events if event["event"] == "finished"))

    # 不完整的分工不能悄悄让协调者闲置，也不能执行一部分后再报错。
    async def test_incomplete_plan_stops_every_member(self):
        agents = [agent([reply(plan(2, 3))]), agent([]), agent([])]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            await collaboration.cooperate(agents, "共同任务", self.memory)
        agents[0]["thread"].run.assert_awaited_once()
        for item in agents[1:]:
            item["thread"].run.assert_not_awaited()

    # 成员失败不重试，保留同伴成果并把失败明确传给 leader。
    async def test_worker_failure_is_reported(self):
        agents = [agent([reply(plan(1, 2, 3)), reply("协调者成果"), reply("有一项失败，仍需补做")]),
                  agent([RuntimeError("连接失败")]), agent([reply("可用成果")])]
        with contextlib.redirect_stdout(io.StringIO()):
            await collaboration.cooperate(agents, "共同任务", self.memory)
        review = json.loads(agents[0]["thread"].run.call_args_list[2].args[0])["instruction"]
        self.assertEqual(review["results"][1], {"agent": 2, "ok": False, "error": "连接失败"})
        self.assertEqual(review["results"][2]["answer"], "可用成果")
        self.assertTrue(all(item["thread"].run.await_count == 1 for item in agents[1:]))
        events = (self.memory.parent / "messages.jsonl").read_text()
        self.assertIn('"event": "failed"', events)

    # 规划格式错误或回合失败时不派工、不汇总，也不重试。
    async def test_bad_plan_stops_before_dispatch(self):
        for answer in (reply(plan(9)), reply(None, TurnStatus.failed)):
            agents = [agent([answer]), agent([])]
            with self.subTest(answer=answer), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises((ValueError, RuntimeError)):
                    await collaboration.cooperate(agents, "共同任务", self.memory)
            agents[0]["thread"].run.assert_awaited_once()
            agents[1]["thread"].run.assert_not_awaited()

    # 汇总失败会保留已完成的执行记录，不重新执行成员任务。
    async def test_review_failure_keeps_work_log(self):
        agents = [agent([reply(plan(1, 2)), reply("协调者成果"), reply(" ")]), agent([reply("已完成成果")])]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            await collaboration.cooperate(agents, "共同任务", self.memory)
        events = [json.loads(line) for line in (self.memory.parent / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "failed")
        self.assertTrue(any(event.get("output") == "已完成成果" for event in events))
        agents[1]["thread"].run.assert_awaited_once()

    # 复用登录与建会话逻辑，仅选中的协调者加载统筹职责，其他成员没有预设角色。
    async def test_team_setup_uses_existing_runtime(self):
        shared_model = object()
        configurations = [{"model": shared_model, "role": ""} for _ in range(25)]
        members = [f"member{number}" for number in range(1, 26)]
        client = AsyncMock()
        client.__aenter__.return_value = client
        with patch.object(collaboration, "AsyncCodex", return_value=client) as factory, \
             patch.object(collaboration, "configure_agents", new=AsyncMock(return_value=configurations)), \
             patch.object(collaboration, "create_memory", return_value=self.memory), \
             patch.object(collaboration, "create_agents", new=AsyncMock(return_value=members)) as create, \
             patch.object(collaboration, "cooperate", new=AsyncMock()) as cooperate:
            await collaboration.run_team("共同任务", 25, 25)
        self.assertIn('forced_login_method="chatgpt"', factory.call_args.kwargs["config"].config_overrides)
        self.assertIn('agents.enabled=false', factory.call_args.kwargs["config"].config_overrides)
        self.assertIn('features.multi_agent=false', factory.call_args.kwargs["config"].config_overrides)
        self.assertIn('features.goals=false', factory.call_args.kwargs["config"].config_overrides)
        self.assertEqual(configurations[0]["role"], "")
        self.assertIn("Leader：单轮分工与审查", configurations[24]["role"])
        self.assertTrue(all(config["role"] == "" for config in configurations[:24]))
        self.assertEqual(len(create.call_args.args[1]), 25)
        self.assertTrue(all(item["model"] is shared_model for item in configurations))
        cooperate.assert_awaited_once_with(members, "共同任务", self.memory, 25)

    # 取消人数上限后，仍须拒绝零人、负数、非整数和无效协调者编号。
    async def test_invalid_team_size_does_not_open_sdk(self):
        with patch.object(collaboration, "AsyncCodex") as client:
            for count, coordinator in ((0, 1), (-1, 1), (1.5, 1), (True, 1), (25, 26), (25, 0)):
                with self.subTest(count=count, coordinator=coordinator), self.assertRaises(ValueError):
                    await collaboration.run_team("任务", count, coordinator)
            client.assert_not_called()

    # 任意成员都能担任协调者，编号 1 也能作为执行成员收到自动角色。
    async def test_coordinator_can_be_any_member(self):
        agents = [agent([reply("成员1成果")]), agent([reply(plan(1, 2, 3)), reply("协调者成果"), reply("整合结果")]), agent([reply("成员3成果")])]
        with contextlib.redirect_stdout(io.StringIO()):
            await collaboration.cooperate(agents, "共同任务", self.memory, coordinator=2)
        calls = agents[1]["thread"].run.call_args_list
        payload = json.loads(calls[0].args[0])
        self.assertEqual(payload["agent"], 2)
        self.assertEqual([row["agent"] for row in payload["instruction"]["members"]], [1, 2, 3])
        self.assertEqual(agents[0]["thread"].run.await_count, 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(calls[1].args[0])["instruction"]["role"], "第2组核算员，关注重复订单")

    # 协调者的执行任务失败也保留其他成员成果，并在同一会话中尝试审核。
    async def test_coordinator_work_failure_is_reported(self):
        agents = [agent([reply(plan(1, 2)), RuntimeError("协调者执行失败"), reply("保留成员2成果，明确缺口")]), agent([reply("其他成果")])]
        with contextlib.redirect_stdout(io.StringIO()):
            await collaboration.cooperate(agents, "共同任务", self.memory)
        review = json.loads(agents[0]["thread"].run.call_args_list[-1].args[0])["instruction"]
        self.assertEqual(review["results"][0], {"agent": 1, "ok": False, "error": "协调者执行失败"})
        self.assertEqual(review["results"][1]["answer"], "其他成果")
        self.assertEqual(agents[0]["thread"].run.await_count, 3)
        agents[1]["thread"].run.assert_awaited_once()

    # 超过旧上限的25人团队全部并行，同一会话不重叠，总回合数为27。
    async def test_large_team_runs_without_overlapping_same_thread(self):
        count = 25
        started, active, gate = set(), set(), asyncio.Event()

        # 协调者完成规划后才能并行执行，全部工作返回后才能进入审核。
        async def run(prompt, **options):
            payload = json.loads(prompt)
            number, phase = payload["agent"], payload["instruction"]["phase"]
            self.assertNotIn(number, active)
            active.add(number)
            try:
                if phase == "plan":
                    assignments = options["output_schema"]["properties"]["assignments"]
                    self.assertEqual(assignments["maxItems"], count)
                    self.assertEqual(assignments["items"]["properties"]["agent"]["maximum"], count)
                    return reply(plan(*range(1, count + 1)))
                if phase == "review":
                    self.assertEqual(started, set(range(1, count + 1)))
                    self.assertEqual(active, {1})
                    return reply("全员汇总")
                started.add(number)
                if len(started) == count:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=1)
                return reply(f"成员{number}成果")
            finally:
                active.remove(number)

        agents = [agent([]) for _ in range(count)]
        for item in agents:
            item["thread"].run.side_effect = run
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = await collaboration.cooperate(agents, "共同任务", self.memory)
        self.assertEqual(result, "全员汇总")
        self.assertEqual(agents[0]["thread"].run.await_count, 3)
        self.assertTrue(all(item["thread"].run.await_count == 1 for item in agents[1:]))
        self.assertIn("最多启动 27 个用户回合", output.getvalue())

    # 单人成员直接处理任务，不产生规划或汇总回合。
    async def test_single_member_answers_directly(self):
        item = agent([reply("直接答案")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = await collaboration.cooperate([item], "共同任务", self.memory)
        self.assertEqual(result, "直接答案")
        item["thread"].run.assert_awaited_once()
        payload = json.loads(item["thread"].run.call_args.args[0])
        self.assertEqual(payload["instruction"]["assignment"], "共同任务")
        self.assertNotIn("output_schema", item["thread"].run.call_args.kwargs)

    # SDK 的工具调用对象应保存为 JSON，便于核对真实写入，且不记录内部推理。
    async def test_tool_trace_is_saved(self):
        result = reply("写入成功")
        result.items = [ThreadItem.model_validate({
            "id": "tool-test", "type": "mcpToolCall", "server": "shared_notes",
            "tool": "update_note", "status": "completed",
            "arguments": {"note_id": "", "expected_version": 0, "text": "待复核"},
        }), SimpleNamespace(type="reasoning")]
        item = agent([result])
        item["thread"].id = "thread-test"
        with contextlib.redirect_stdout(io.StringIO()):
            await collaboration.cooperate([item], "共同任务", self.memory)
        rows = [json.loads(line) for line in (self.memory.parent / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["thread_id"], "thread-test")
        self.assertEqual(rows[-1]["turn_id"], "test")
        self.assertEqual(len(rows[-1]["tool_calls"]), 1)
        self.assertEqual(rows[-1]["tool_calls"][0]["tool"], "update_note")


if __name__ == "__main__":
    unittest.main()
