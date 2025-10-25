# agent.py
# 最终修复版，强化了分类指令以确保输出格式正确

import json
import os
from google.adk.agents import LoopAgent, LlmAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.tool_context import ToolContext

# --- 导入 agent_tools.py 中的所有工具 ---
from agent_tools import (
    get_next_error_log,
    mark_log_as_processed_by_rename,
    parse_error_log,
    find_sha_for_timestamp,
    checkout_oss_fuzz_commit,
    run_fuzz_build_streaming,
    read_file_content,
    update_reproduce_table
)

# --- 全局常量 ---
DPSEEK_API_KEY = os.getenv("DPSEEK_API_KEY", "YOUR_DEEPSEEK_API_KEY")
MODEL = "deepseek/deepseek-coder"
COMMITS_FILE_PATH = "/root/reproduce_note_fuzz/oss-fuzz_information_obtain/github_commits.txt"

# --- 工具定义：循环控制 ---
def exit_loop(tool_context: ToolContext):
    """当所有日志处理完毕时，调用此工具以终止主循环。"""
    print(f"--- [Tool Call] exit_loop triggered by {tool_context.agent_name} ---")
    tool_context.actions.escalate = True
    return {"status": "Loop exit signal sent."}

# --- Agent 定义 ---

# 1. 配置收集 Agent (不变)
config_collector_agent = LlmAgent(
    name="config_collector_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    你是一个设置助手。你的唯一任务是友好地向用户提问，要求他们提供两项信息：
    1. OSS-Fuzz 项目的根目录绝对路径。
    2. 存储错误日志的根目录路径。
    请直接输出你的问题，不要包含任何其他多余的问候或解释。
    """,
)

# 2. 配置解析 Agent (不变)
config_parser_agent = LlmAgent(
    name="config_parser_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    用户的上一条消息包含了 OSS-Fuzz 项目和错误日志的路径。
    你的任务是从用户的输入中提取这两个路径，并生成一个包含 "oss_fuzz_path" 和 "logs_directory" 键的 JSON 字符串。
    """,
    output_key="initial_config",
)

# 3. Build Fuzzer Agent (不变)
build_fuzzer_agent = LlmAgent(
    name="build_fuzzer_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=f"""
    你的任务是复现一个 OSS-Fuzz 项目的构建错误。请严格按照以下步骤操作：
    1.  调用 `get_next_error_log` 工具，传入 `logs_directory`。
    2.  如果 `status` 是 'finished'，立即调用 `exit_loop`。
    3.  否则，调用 `parse_error_log` 提取 `project_name` 和 `error_date`。
    4.  调用 `find_sha_for_timestamp` 找到 `sha`。
    5.  调用 `checkout_oss_fuzz_commit` 切换代码版本。
    6.  调用 `run_fuzz_build_streaming` 执行构建。
    7.  将所有收集到的信息整合为一个JSON字符串作为最终输出。
    """,
    tools=[
        get_next_error_log,
        exit_loop,
        parse_error_log,
        find_sha_for_timestamp,
        checkout_oss_fuzz_commit,
        run_fuzz_build_streaming
    ],
    output_key="build_attempt_result",
)

# 4. Classify Agent (【核心修复】: 采用全新的、更严格的指令)
classify_agent = LlmAgent(
    name="classify_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=8192),
    instruction="""
    你是一个极其严谨的软件构建错误分析专家。你的任务是精确地分析并记录构建失败的原因。
    上一步的输出 `build_attempt_result` 是一个包含所有信息的 JSON 字符串。

    **请严格遵循以下步骤：**
    **在处理完成之后必须进行标记**
    1.  **解析输入**: 首先，解析 `build_attempt_result` 中的 JSON 数据。

    2.  **检查状态**: 查看 JSON 中的 `status` 字段。
        - 如果 `status` 是 'success'，立即调用 `mark_log_as_processed_by_rename` 工具，`log_path` 参数必须来自 JSON。然后任务结束。
        - 如果 `status` 是 'error'，继续下一步。

    3.  **读取日志**: 调用 `read_file_content` 两次，分别读取原始日志和新构建日志。

    4.  **判断并精确分类**:
        a.  比较两个日志内容，判断错误是否一致（'是'或'否'）。
        b.  根据**新的构建日志**内容，从下面的列表中选择**最贴切的一个**子分类。
        c.  **【极其重要】** 为 `update_reproduce_table` 工具准备 `category` 参数时，其格式**必须**是**主分类**和**子分类**的组合，中间用换行符 `\n` 分隔。

        **分类列表:**
        - 环境问题
            - RC1：编译器问题
            - RC2：覆盖文件及目录问题
            - RC3：项目环境问题
            - RC4：网络问题
            - RC5：硬件问题
            - RC6：权限问题
        - 语料相关问题
            - RC7：语料相关问题
        - 下载外部资源问题
            - RC8：下载外部资源时出错
        - 项目依赖问题
            - RC9：项目依赖问题
        - 构建与配置问题
            - RC10：项目配置与构建文件问题
            - RC11：覆盖构建配置及文件问题
            - RC12：模糊测试构建脚本问题
        - 源码相关问题
            - RC13：源码相关的项目编译错误
            - RC14：缺失源码文件
        - 命令与参数相关问题
            - RC15：命令与参数相关问题
        - 模糊测试运行时问题
            - RC16：模糊测试运行时问题
        - 信息不足
            - RC17：信息不足
        - Fuzz 目标问题
            - RC18：Sanitizer 错误
            - RC19：损坏的 Fuzz 目标
            - RC20：缺失 Fuzz 目标
        - 其他
            - RC21-RC25

        **格式示例**: 如果错误是关于项目配置的，那么 `category` 参数的值**必须**是字符串 `"构建与配置问题\nRC10：项目配置与构建文件问题"`。

    5.  **记录到表格**: 调用 `update_reproduce_table` 工具。所有参数（`project_name`, `date`, `category`, `failure_reason`, `is_consistent`）都必须来自你的分析结果。

    6.  **最终标记**: 无论记录是否成功，最后都**必须**调用 `mark_log_as_processed_by_rename` 工具，`log_path` 参数必须来自 JSON。

    """,
    tools=[
        read_file_content,
        update_reproduce_table,
        mark_log_as_processed_by_rename,
    ],
)


# --- 工作流定义 (不变) ---

main_loop_agent = LoopAgent(
    name="main_loop_agent",
    sub_agents=[
        build_fuzzer_agent,
        classify_agent,
    ],
    max_iterations=1000
)

root_agent = SequentialAgent(
    name="oss_fuzz_reproduce_workflow",
    sub_agents=[
        config_collector_agent,
        config_parser_agent,
        main_loop_agent
    ],
)
