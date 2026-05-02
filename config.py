import os
from dotenv import load_dotenv

load_dotenv()

AGENTPATH = "/MinimalCodeAgent"
REPOPATH = "/testbed"
ENV_NAME = "minimalcodeagent"
AGENTPORT = 8080
MODEL = "gemini"
OLD_ENV_NAME = "testbed"

# Docker API 客户端读超时（秒），默认 10 分钟，避免 containers.create 等操作超时
DOCKER_CLIENT_TIMEOUT = int(os.environ.get("DOCKER_CLIENT_TIMEOUT", "600"))

# ---------- Agent 配置 ----------
AGENT_NAME = os.environ.get("CCB_AGENT_NAME", "minimal-code-agent")
HARBOR_PATH = os.environ.get("HARBOR_PATH", os.path.join(os.path.dirname(__file__), "harbor"))

# ---------- Harbor 对齐配置 ----------
HARBOR_TASKS_DIR = os.environ.get("CCB_HARBOR_TASKS_DIR", "harbor_tasks")
DEFAULT_AGENT = os.environ.get("CCB_DEFAULT_AGENT", "claude-code")
DEFAULT_AGENT_TIMEOUT_SEC = int(os.environ.get("CCB_AGENT_TIMEOUT", "3600"))   # 60 min
DEFAULT_VERIFIER_TIMEOUT_SEC = int(os.environ.get("CCB_VERIFIER_TIMEOUT", "1200"))  # 20 min
DEFAULT_BUILD_TIMEOUT_SEC = int(os.environ.get("CCB_BUILD_TIMEOUT", "1800"))   # 30 min

# ---------- 支持的 Benchmark ----------
SUPPORTED_BENCHMARKS = ["swebench", "swebench-pro"]

# ---------- 支持的语言 ----------
SUPPORTED_LANGUAGES = ["py", "js", "java", "go", "c", "php", "rb", "rs"]

# ---------- 语言-测试框架映射 ----------
LANGUAGE_TEST_FRAMEWORK = {
    "py": "pytest / unittest",
    "js": "jest / mocha / vitest",
    "java": "JUnit / Maven Surefire",
    "go": "go test",
    "c": "make test / CTest",
    "php": "PHPUnit",
    "rb": "RSpec / Minitest",
    "rs": "cargo test",
}

# ---------- 语言-显示名称映射 ----------
LANGUAGE_DISPLAY_NAME = {
    "py": "Python",
    "js": "JavaScript/TypeScript",
    "java": "Java",
    "go": "Go",
    "c": "C/C++",
    "php": "PHP",
    "rb": "Ruby",
    "rs": "Rust",
}

# ---------- 语言-环境配置指引 ----------
LANGUAGE_ENV_INSTRUCTIONS = {
    "py": "配置名为 `testbed` 的 conda 环境，在其中安装所有 Python 依赖。你可以使用 `pip install -e .` 或根据 setup.py/pyproject.toml 安装。",
    "js": "运行 `npm install`（或 `yarn install` / `pnpm install`）安装 Node.js 依赖。确保 node_modules 正确安装。检查 package.json 中的依赖。",
    "java": "运行 `mvn install -DskipTests`（或 `gradle build -x test`）安装 Maven/Gradle 依赖。确保所有 Java 依赖正确下载。",
    "go": "运行 `go mod download` 下载 Go 模块依赖。然后运行 `go build ./...` 确保项目能编译。",
    "c": "运行 `./configure && make` 或 `cmake . && make` 等构建命令。安装缺失的系统依赖（使用 apt-get）。",
    "php": "运行 `composer install` 安装 PHP 依赖。确保所有 Composer 包正确安装。",
    "rb": "运行 `bundle install` 安装 Ruby gems。确保 Gemfile.lock 正确处理。",
    "rs": "运行 `cargo build` 编译项目并下载 crates 依赖。确保项目能成功编译。",
}

# ---------- 语言-可用工具链描述 ----------
LANGUAGE_AVAILABLE_TOOLS = {
    "py": "Python 3.x, pip, conda (miniconda3)",
    "js": "Node.js, npm, pnpm, NVM, Chrome (for browser testing)",
    "java": "JDK, Maven (mvn), Ant, mvnd",
    "go": "Go toolchain (go build, go test, go mod)",
    "c": "gcc, make, cmake, autoconf, bison, flex, python3",
    "php": "PHP, Composer, gd/zip/gmp/curl extensions",
    "rb": "Ruby, Bundler, gem",
    "rs": "Rust, Cargo",
}
DevGeneratePrompt = """\'你是一个专业的编程教育 Agent，你的任务是基于 SWE-bench 数据和 Git 仓库，自动生成一个编程练习题。

**重要约束：你只能读取和分析代码，不能修改任何代码文件。你唯一可以写入的文件是 `/testbed/problem.json`。**

请严格按照以下步骤执行操作：

第一步：读取 SWE-bench 数据

1. 读取文件 `/testbed/swe_data.json`，获取以下信息：
   - `base_commit`: 基础 commit hash（题目开始时的代码状态）
   - `problem_statement`: 原始问题描述（GitHub issue 的内容）
   - `instance_id`: 实例 ID
   - `repo`: 仓库名称

2. 查看当前 Git 仓库状态：
   - 使用 `git rev-parse HEAD` 获取当前 HEAD 的 commit hash（这是解决 issue 的 merge commit）
   - 使用 `git log --oneline -1` 查看当前 commit 信息，确认这是一个 merge commit

第二步：分析代码变化

1. 比较当前 HEAD（resolved commit）与 base_commit 的差异：
   - 使用 `git diff {base_commit} HEAD` 查看所有文件的变化
   - 使用 `git diff --stat {base_commit} HEAD` 查看变化的文件列表
   - 重点关注修改的核心代码文件（排除测试文件、文档文件等）

2. 理解变化的目的：
   - 结合 `problem_statement` 中的问题描述，理解这些代码变化是为了解决什么问题
   - 识别关键的修改点：是修复 bug、添加功能、重构代码还是其他

第三步：生成题目描述 (problem.json)

基于你的分析，构建一个 JSON 对象：

- **Title**: 基于 problem_statement 和代码变化，总结题目的核心内容
- **Background**: 结合 problem_statement 描述问题的背景和当前代码状态（base_commit 时的状态）
- **Task**: 指导用户重新实现解决该问题所需的代码修改。指出修改的目的，用户可以根据需要自行选择修改哪些文件
- **Validation**: 包含两个字段：
  - `description`: 描述如何验证代码是否正确（可以参考 problem_statement 中的测试要求）
  - `eval_script_path`: eval.sh 脚本的路径（相对于 `/testbed` 目录，例如 `eval.sh`）。注意：你不需要实际创建这个文件，只需要在 JSON 中指定路径即可。系统会自动处理脚本的创建和保存。

第四步：输出结果

1. **重要：不要修改任何代码文件，不要执行任何 git checkout 或 git reset 命令**
2. 将生成的 JSON 对象写入 `/testbed/problem.json` 文件（这是你唯一可以写入的文件）
3. 执行完成后，回复"题目生成完毕"

输出格式示例：
生成的 problem.json 必须严格遵守以下 JSON 结构：

```json
{
  "title": "修复数据处理器的 JSON 解析异常处理",
  "background": "用户报告当输入格式错误的 JSON 数据时，data_processor.py 中的 process_user_data 函数会直接崩溃，导致服务不可用。当前代码（base_commit）未处理 JSON 解码失败的情况。",
  "task": "修复 JSON 解析异常处理问题。当输入格式错误的 JSON 数据时，process_user_data 函数应该能够安全处理并返回错误信息，而不是直接崩溃。你可以根据需要修改任何相关文件来完成这个任务。",
  "validation": {
    "description": "运行单元测试验证修复是否正确。测试将检查：1) 输入合法 JSON 时能正确解析；2) 输入非法 JSON 时能安全返回错误信息而不崩溃。",
    "eval_command": ["bash eval.sh"]
  }
}
```

重要提示：
- **禁止修改任何代码文件，禁止执行 git checkout、git reset 等命令**
- 只能读取和分析代码，只能写入 `/testbed/problem.json` 文件
- 必须基于 problem_statement 和实际的代码差异来生成题目
- Title 和 Background 要准确反映 problem_statement 的内容
\'"""

TestcaseGeneratePrompt = """\'
你是一个软件测试与 QA 教育 Agent。你的任务是基于 SWE-bench 数据和 Git 仓库，生成一个"编写单元测试"的编程练习题。

**重要约束：你只能读取和分析代码，不能修改任何代码文件。你唯一可以写入的文件是 `/testbed/problem.json`。**

请严格按照以下步骤执行：

第一步：读取 SWE-bench 数据

1. 读取文件 `/testbed/swe_data.json`，获取以下信息：
   - `base_commit`: 基础 commit hash（题目开始时的代码状态）
   - `problem_statement`: 原始问题描述（GitHub issue 的内容）
   - `instance_id`: 实例 ID
   - `repo`: 仓库名称
   - `FAIL_TO_PASS`: 需要从失败变为通过的测试用例列表
   - `PASS_TO_PASS`: 需要保持通过的测试用例列表

2. 查看当前 Git 仓库状态：
   - 使用 `git rev-parse HEAD` 获取当前 HEAD 的 commit hash（这是解决 issue 的 merge commit）
   - 使用 `git log --oneline -1` 查看当前 commit 信息，确认这是一个 merge commit

第二步：分析代码变化和测试需求

1. 比较当前 HEAD（resolved commit）与 base_commit 的差异：
   - 使用 `git diff {base_commit} HEAD` 查看所有文件的变化
   - 使用 `git diff --stat {base_commit} HEAD` 查看变化的文件列表
   - 重点关注新增或修改的功能代码文件（通常在 `src/`、`lib/` 等目录，排除测试文件）

2. 识别需要测试的代码：
   - 从代码变化中识别新增的函数、类或修改的核心逻辑
   - 结合 `problem_statement` 中的问题描述，理解这些代码变化是为了解决什么问题
   - 确定应该为哪些函数、类或功能模块编写测试

3. 检查测试文件状态：
   - 查看是否有对应的测试文件（通常在 `tests/`、`test/` 目录）
   - 使用 `git diff {base_commit} HEAD -- tests/` 查看测试文件的变化
   - 如果测试文件在 resolved commit 中已存在，需要查看其内容以了解测试要求

第三步：生成题目描述 (problem.json)

基于你的分析，构建一个 JSON 对象：

- **Title**: 基于 problem_statement 和代码变化，总结测试题目的核心内容（例如"为 xxx 函数编写单元测试"）
- **Background**: 结合 problem_statement 描述问题的背景，说明功能代码已实现（在最新一次 commit 中），但需要编写测试来验证其正确性
- **Task**: 指导用户编写测试用例来验证代码的正确性。指出测试的目的和需要覆盖的场景，用户可以根据需要自行选择或创建测试文件。可以参考 `FAIL_TO_PASS` 和 `PASS_TO_PASS` 中的测试用例来理解需要测试的功能
- **base_commit**: base_commit 的 hash 值（从 swe_data.json 中读取），这是一个单独的字段
- **Validation**: 包含两个字段：
  - `description`: 你需要生成一个 eval.sh，需要完成对 background 里面功能点的测试。你需要保证目前的 code repo 运行 eval.sh 可以通过所有单测点。请注意目前的 code repo 是开发完毕的，请你仅对上述描述的功能点新增单测、不要去修改功能代码逻辑。
  - `eval_command`: 包含一个命令列表，例如 `["bash eval.sh"]`

第四步：输出结果

1. **重要：不要修改任何代码文件，不要执行任何 git checkout 或 git reset 命令**
2. 将生成的 JSON 对象写入 `/testbed/problem.json` 文件（这是你唯一可以写入的文件）
3. 执行完成后，回复"单测题目生成完毕"

输出格式示例：
生成的 problem.json 必须严格遵守以下 JSON 结构：

```json
{
  "title": "为数据处理器的 JSON 解析功能编写单元测试",
  "background": "在base_commit状态下，用户报告当输入格式错误的 JSON 数据时，data_processor.py 中的 process_user_data 函数会直接崩溃。为了解决这个问题，在最新一次 commit 中已经实现了异常处理逻辑。现在需要编写单元测试来验证这个修复是否正确工作。",
  "task": "编写测试用例来验证 process_user_data 函数的异常处理逻辑。测试应该覆盖正常情况、异常情况和边界情况，确保函数能够正确处理各种输入。你可以根据需要创建或修改任何测试文件来完成这个任务。",
  "base_commit": "abc123def456...",
  "validation": {
    "description": "你需要生成一个 eval.sh，需要完成对 background 里面功能点的测试。你需要保证目前的 code repo 运行 eval.sh 可以通过所有单测点。请注意目前的 code repo 是开发完毕的，请你仅对上述描述的功能点新增单测、不要去修改功能代码逻辑。",
    "eval_command": ["bash eval.sh"]
  }
}
```

重要提示：
- **禁止修改任何代码文件，禁止执行 git checkout、git reset 等命令**
- 只能读取和分析代码，只能写入 `/testbed/problem.json` 文件
- 必须基于 problem_statement 和实际的代码差异来生成测试题目
- 重点关注新增或修改的功能代码，确定需要测试的核心逻辑
- Title 和 Background 要准确反映 problem_statement 的内容和测试需求
- 可以参考 `FAIL_TO_PASS` 和 `PASS_TO_PASS` 中的测试用例来理解需要测试的功能
\'
"""

SolveDevelopmentPrompt = """\'# Role
你是一个高级软件工程师 Agent。你的目标是根据提供的任务描述文件，修复代码中的问题并完成功能开发。

# Context
当前仓库根目录下包含一个名为 `problem.json` 的文件，其中定义了你需要完成的编程任务。为了方便你直接开始工作，我已经读取了该文件的内容并在下方给出。你需要根据 task 描述实现功能代码的修改，使代码能够通过测试。

# Data Schema Definition
在执行任务前，请先理解 JSON 数据中各个字段的含义：
- **title**: 题目的名称。
- **background**: 描述当前代码的状态、背景知识或存在的具体问题（Bug/缺陷）。
- **task**: 你需要执行的具体操作，包括逻辑实现的详细要求。你可以根据需要修改任何相关文件来完成这个任务。
- **validation**: 验证代码正确性的方法，包含描述和 eval_command

# Task Description (Content of problem.json)
以下是本次任务的具体内容：

```json
{problem_json}
```

# Instructions
请仔细阅读 problem.json 中的内容，理解任务要求，然后：
1. 分析任务背景和需求
2. 完成相应的代码修改，实现所需的功能
3. 确保修改后的代码能够通过 validation 中指定的测试（系统会运行 eval_command 来验证）
4. 完成后，简要回复"任务完成"
\'"""

SolveTestCasePrompt = """\'# Role
你是一个软件测试与 QA 工程师 Agent。你的目标是根据提供的任务描述文件，编写测试用例来测试修改后的代码。

# Context
当前仓库根目录下包含一个名为 `problem.json` 的文件，其中定义了你需要完成的测试编写任务。为了方便你直接开始工作，我已经读取了该文件的内容并在下方给出。

**重要提示**：
- 当前代码状态：代码已经 checkout 到 base_commit，并且已经应用了功能代码的修改（patch），但测试相关的修改已被移除。
- 你需要编写测试用例，并创建一个 `eval.sh` 脚本。
- **eval.sh 必须是一个纯测试脚本，只包含测试命令（如 pytest、unittest 等），不能包含任何 git 操作（如 git checkout、git diff、git reset 等）**。
- 系统会在不同的代码状态下运行这个脚本来验证你的测试是否正确：
  - 在 base_commit 状态下运行应该失败（因为功能代码还未修复）
  - 在应用了功能代码 patch 后（initial_commit状态下）运行应该通过（因为功能代码已修复）

# Data Schema Definition
在执行任务前，请先理解 JSON 数据中各个字段的含义：
- **title**: 题目的名称。
- **background**: 描述当前代码的状态、背景知识或需要测试的功能。
- **task**: 你需要执行的具体操作，包括需要编写的测试用例要求。你可以根据需要修改或创建任何测试文件。
- **base_commit**: base_commit 的 hash 值（系统用于评估，你不需要在 eval.sh 中使用）
- **resolved_commit**: resolved_commit 的 hash 值（系统用于评估，你不需要在 eval.sh 中使用）
- **validation**: 验证测试正确性的方法，包含：
  - `description`: 验证逻辑的描述
  - `eval_command`: 包含测试命令的列表

# Task Description (Content of problem.json)
以下是本次任务的具体内容：

```json
{problem_json}
```

# Instructions
请仔细阅读 problem.json 中的内容，理解任务要求，然后：
1. 分析任务背景和需求，理解需要测试的功能
2. 编写测试用例（可以修改或创建测试文件）
3. 创建 eval.sh 脚本，该脚本应该只包含测试命令（如 pytest tests/test_file.py、python -m unittest tests.test_file 等）
4. **重要：eval.sh 必须是纯测试脚本，不能包含任何 git 操作（如 git checkout、git diff、git reset 等）**
5. 系统会在不同的代码状态下运行 eval.sh 来验证你的测试是否正确
6. 完成后，简要回复"任务完成"
\'"""

SolveEnvironmentPrompt = """\'# Role
你是一个 DevOps 和环境配置工程师 Agent。你的目标是根据提供的任务描述文件，配置项目运行所需的环境。

# Context
当前仓库根目录下包含一个名为 `problem.json` 的文件，其中定义了你需要完成的环境配置任务。为了方便你直接开始工作，我已经读取了该文件的内容并在下方给出。

**重要提示**：
- 当前代码状态：代码已经 checkout 到 base_commit并应用了功能代码和测试代码的修改（patch），但环境相关的配置文件已被移除。
- 你需要创建环境配置脚本（如 setup.sh），配置名为 `testbed` 的 conda 环境，并在该环境中安装所需的依赖。

# Data Schema Definition
在执行任务前，请先理解 JSON 数据中各个字段的含义：
- **title**: 题目的名称。
- **background**: 描述当前代码的状态、背景知识或环境配置需求。
- **task**: 你需要执行的具体操作，包括环境配置的详细要求。你需要创建 setup.sh 脚本来配置环境。
- **validation**: 验证环境配置正确性的方法，包含描述和 eval_command

# Task Description (Content of problem.json)
以下是本次任务的具体内容：

```json
{problem_json}
```

# Instructions
请仔细阅读 problem.json 中的内容，理解任务要求，然后：
1. 分析任务背景和需求，理解需要配置的环境
2. 分析源代码，找出所有引用的第三方依赖库
3. 创建 setup.sh 脚本，配置名为 `testbed` 的 conda 环境，并在该环境中安装所有依赖
4. 确保脚本运行结束后，`testbed` 环境满足项目运行条件，能够通过所有相关的测试用例
5. 完成后，简要回复"任务完成"
\'"""

# ---------- 多语言 Environment Prompt 模板 ----------
SolveEnvironmentPromptTemplate = """\'# Role
你是一个 DevOps 和环境配置工程师 Agent。你的目标是根据提供的任务描述文件，配置项目运行所需的环境。

# Context
当前仓库已经 checkout 到 base_commit 并应用了功能代码和测试代码的 patch。
但项目运行环境尚未搭建。

**当前语言: {language_display}**
**可用工具链: {available_tools}**

# Task
{task_content}

# 环境配置要求
你需要创建 setup.sh 脚本来配置项目运行环境：

{language_specific_instructions}

# Task Description (Content of problem.json)
以下是本次任务的具体内容：

```json
{{problem_json}}
```

# Instructions
请仔细阅读 problem.json 中的内容，理解任务要求，然后：
1. 分析任务背景和需求，理解需要配置的环境
2. 分析源代码，找出所有引用的第三方依赖库
3. 创建 setup.sh 脚本，安装所有必要的依赖
4. 确保脚本运行结束后，项目能够通过所有相关的测试用例
5. 完成后，简要回复"任务完成"
\'"""


# ---------- Prompt 获取函数 ----------

def get_generate_prompt(problem_type: str, language: str = "py") -> str:
    """
    根据题目类型和语言获取出题 prompt。

    Args:
        problem_type: 题目类型（Development, TestCase, Environment, FullPipe）
        language: 语言标识

    Returns:
        prompt 字符串
    """
    if problem_type == "Development":
        return DevGeneratePrompt
    elif problem_type == "TestCase":
        return TestcaseGeneratePrompt
    elif problem_type in ("Environment", "FullPipe"):
        return DevGeneratePrompt  # 出题时复用 Development 的 prompt
    else:
        return DevGeneratePrompt


def get_solve_prompt(problem_type: str, language: str = "py") -> str:
    """
    根据题目类型和语言获取解题 prompt。

    Args:
        problem_type: 题目类型（Development, TestCase, Environment, FullPipe）
        language: 语言标识

    Returns:
        prompt 字符串
    """
    if problem_type == "Development":
        return SolveDevelopmentPrompt
    elif problem_type == "TestCase":
        return SolveTestCasePrompt
    elif problem_type == "Environment":
        return _get_environment_solve_prompt(language)
    elif problem_type == "FullPipe":
        return _get_environment_solve_prompt(language)
    else:
        return SolveDevelopmentPrompt


def _get_environment_solve_prompt(language: str) -> str:
    """
    根据语言生成 Environment 类型的解题 prompt。

    对于 Python，使用原始的 SolveEnvironmentPrompt（基于 conda）。
    对于非 Python 语言，使用模板化的 prompt。
    """
    if language == "py":
        return SolveEnvironmentPrompt

    language_display = LANGUAGE_DISPLAY_NAME.get(language, language)
    available_tools = LANGUAGE_AVAILABLE_TOOLS.get(language, "N/A")
    lang_instructions = LANGUAGE_ENV_INSTRUCTIONS.get(language, "请根据项目配置文件安装所有依赖。")

    return SolveEnvironmentPromptTemplate.format(
        language_display=language_display,
        available_tools=available_tools,
        task_content="配置项目运行环境，安装所有必要的依赖，使项目能够通过测试。",
        language_specific_instructions=lang_instructions,
    )