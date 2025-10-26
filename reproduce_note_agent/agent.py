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

# 1. & 2. 配置收集与解析 Agent
config_collector_agent = LlmAgent(
    name="config_collector_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    你是一个设置助手。你的唯一任务是友好地向用户提问，要求他们提供两项信息：
    1. OSS-Fuzz 项目的根目录绝对路径。
    2. 存储错误日志的根目录路径。
    """,
)
config_parser_agent = LlmAgent(
    name="config_parser_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction="""
    用户的上一条消息包含了 OSS-Fuzz 项目和错误日志的路径。
    你的任务是从用户的输入中提取这两个路径，并生成一个包含 "oss_fuzz_path" 和 "logs_directory" 键的 JSON 字符串。
    """,
    output_key="initial_config",
)

# 3. Build Fuzzer Agent
build_fuzzer_agent = LlmAgent(
    name="build_fuzzer_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY),
    instruction=f"""
    你的任务是复现一个 OSS-Fuzz 项目的构建。**你的职责仅限于此。**
    严格按以下步骤操作：
    1.  调用 `get_next_error_log` 工具。
    2.  如果工具返回 `status: 'finished'`，你必须立即调用 `exit_loop` 工具，然后你的任务就结束了。
    3.  否则，按顺序调用 `parse_error_log`, `find_sha_for_timestamp`, `checkout_oss_fuzz_commit`, 和 `run_fuzz_build_streaming`。
    4.  将所有收集到的信息整合为一个JSON字符串作为你的最终输出。
    5.  **【极其重要】** 在输出 JSON 后，你的任务就**绝对结束**了。**不要**进行任何额外的思考，**不要**调用任何其他工具，**不要**对下一步做什么发表任何评论。
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

# 4. Classify Agent
classify_agent = LlmAgent(
    name="classify_agent",
    model=LiteLlm(model=MODEL, api_key=DPSEEK_API_KEY, max_output_tokens=8192),
    instruction="""
    你是一个分析和标记专家。**你的唯一职责**是处理上一步 `build_fuzzer_agent` 输出的 `build_attempt_result` JSON 字符串。

    **请严格遵循以下逻辑，不要有任何偏差：**

    1.  **解析输入**: 首先，解析 `build_attempt_result` 中的 JSON 数据。

    2.  **检查状态并行动**: 查看 JSON 中的 `status` 字段。
        - **如果 `status` 是 'success'**:
            a. 你**必须**立即调用 `mark_log_as_processed_by_rename` 工具。`log_path` 参数的值**必须**来自你解析的 JSON。
            b. 调用工具后，你的任务就此结束。

        - **如果 `status` 是 'error'**:
            a. 调用 `read_file_content` 两次，读取原始日志和新构建日志。
            b. 比较日志，判断错误是否一致（'是'或'否'）。
            c. **根据新日志，从下面的列表中选择最贴切的一个子分类**，并总结失败原因。
            d. **【极其重要】** 为 `update_reproduce_table` 工具准备 `category` 参数时，其格式**必须**是**主分类**和**子分类**的组合，中间用换行符 `\n` 分隔。

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

            e. 调用 `update_reproduce_table` 工具，记录所有分析结果。
            f. **最后，无论记录是否成功，都必须调用 `mark_log_as_processed_by_rename` 工具**，`log_path` 参数必须来自 JSON。
            g. 调用工具后，你的任务就此结束。
    """,
    tools=[
        read_file_content,
        update_reproduce_table,
        mark_log_as_processed_by_rename,
    ],
)


# --- 工作流定义 ---

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

