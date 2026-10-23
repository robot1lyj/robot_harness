# 在同一使用项目实施、运行与改进

当前项目的根不是框架工程。用安装后的 `robot-rsi init . --core-wheel CORE.whl --source SOURCE`
建立独立使用环境，保留当前路径与用户文件。coding 宿主先读 `robot-rsi guide agent`，直接使用
已有编辑/终端工具实施；内置 source-only agent run 继续只读，不代替 coding 工具。
配置在 profiles，扩展在 extensions/skills，资产在 assets，新状态默认 .rsi/state。
CLI 未显式选择时实际读取最近的 tool.robot-rsi.profile，默认运行目录和任务沿该 Profile；
显式 --profile/ROBOT_RSI_PROFILE 优先。同一个Agent按工作需要选择宿主已有的Profile/资料范围，不认领或切换身份；原role/Store隔离保持，文本不授新权限。既有对话不会因目的变化清空，只有独立评估声明需要干净上下文。

接入同时交付实际能力/资源目录和日常技能目录。先用 capabilities_list/resources_list 与
skills_list/skill_describe 看当前事实，再执行安装的代码技能，例如状态/相机观测及时效缺项报告。
同一 Runtime 保持连接、等待、存证与结束；多个一次性 CLI 不能共享活连接。后续普通任务按需读取
有效事实和技能，不再重复源码接入分析。详见[接入与最小调用](integration.md)。

更新框架只改变当前项目的固定依赖、锁和安装，不覆盖本地实现/配置/记忆。保留旧 wheel 和任务引用，
在正常任务边界更新；项目内改进和公共框架缺口仍按下面已有机制处理。

## 开发完成、提交与接续

一次可独立交付的功能或修复完成时，执行必要验证，更新原事实文档和当前短检查点，然后在正确仓库/分支
做本地 Git 提交，报告实际 SHA、已验范围和未决项。普通观测/运行不执行 Git，不提交数据库、认证或原始日志；
不是每次编辑都提交。明确要求不提交时遵从，无变化不空提交，WIP 不冒充完成。提交不等于验收、安装或推送。

先检查当前 worktree/分支及已暂存、未暂存内容，只纳入本次路径或必要片段。独立且完全属于本次的文件可用：

```bash
git status --short
git branch --show-current
git add -- src/local_fix.py docs/local_method.md docs/progress.md
git commit --only -m "Fix local method and record validation" -- src/local_fix.py docs/local_method.md docs/progress.md
git rev-parse HEAD
git status --short
```

这里的路径只是示例，须替换为实际文件；`--only` 避免带入其他路径的暂存内容。同一文件混有他人修改时，
用已有 Git 工具选择本次片段或交接宿主处理，不整文件顺带提交。缺 Git、身份、仓库或写权限时，保留成果并
报告具体条件与“提交未完成”；不虚构 SHA、不改全局身份、不在已有 worktree 中另建仓库。

受控入口允许独立仓库的项目内 `.git` 写入；worktree 的 `.git` 指针若指向项目外公共元数据，
受控进程不因此获得公共目录权限。下述 coding 入口的本地父进程会完成明确范围的宿主提交；其他宿主
需沿自己的终端接续。没有宿主执行能力则如实留待办，不扩大挂载、不建特权 Git 服务。

接续使用已有 `workspace_context`、`project_read` 和 `workspace_read`：已知 owner/键直接读，缺定位才检索。
恢复目标、最新约束、已完成及验证范围、未决/未知、下一步和来源，够用就继续。先维护原事实再替换短摘要；
没有相关变化不重写，完成过程转为已有历史引用，不强制新目录或 MEMORY 文件。Git 保存代码差异，记忆只保留
决策与继续路径，不复制代码/日志。已有提交可引用，本次 SHA 在提交后交付，不为回写自身 SHA 再循环提交。
有用失败保留症状、尝试、观察和重试条件，假设不变事实；只在相关行为/版本/条件变化时重验受影响部分。
资料按原Profile/用途隔离；同一已授开发与运行工具的Agent可按需读局部改进资料，普通执行只需适用事实/方法。便签不代替环境、配置或设备状态owner。

### 已接入的本地 coding 收尾

下文固定安装的 `launch.py coding` 已接开始/结束处理。它保留原 `codex exec` 的 JSON 事件和明确 session ID，
启动时记录当前项目/分支/HEAD与已有改动。末尾只收一份结构化交接：本次源码路径、已有验证依据、实际提交、
可恢复的短检查点和未决项。检查点可以直接在原 development Store 的 `coding_delivery` 记录中，不要求新文件树
或固定文档改动。验证标明宿主观察的成功命令或 Agent 报告的可读引用/不适用说明；收尾不会替你重跑测试。

空清单或runtime标签不代表代码没改。已有文件事件或轻量Git事实显示源码变化时，补齐本次交接，或用
`unrelated_changes`的path/reason明确说明他人改动，不自动认领。无关文件/暂存可继续推进，收尾不冻结开始快照；
本次路径原有混合改动仍按明确范围保留。

独立仓库由受控 Agent 提交。worktree 本地父进程只用固定 Git 操作，提交明确属于本次且开始时干净的文件，
不运行项目脚本、过滤器或钩子，不开放公共 `.git`。原暂存/无关内容保持；同文件已有混合改动或提交归属不清时，
留下具体路径待办，不能整文件吞并。缺身份/工具/仓库如实报告，不伪造 SHA。

正常交付不增加模型回合。只有缺项才反馈同一 session，默认最多自动补一回合；仍缺返回码2并保留待办。
进程退出码0不单独表示交付完成；末尾 `coding.delivery` 的 complete/pending/interrupted 才是该接缝结果。
中断返回130，不自动恢复；无源码变化不空提交，明确不提交可在外层加 `--no-commit`。普通 `launch.py run` 和
机器人 task_finish 不加 Git 门禁；未接此收尾的桌面/仅MCP会话仍用手动方法，不能视为已接管。

用末尾记录中的原 session ID 查看或显式接续（保留原模型/权限，不使用 `--last`）：

```bash
/path/host/core-0.4.0-v1/launch.py coding --project /project --resume-session SESSION_ID --status
/path/host/core-0.4.0-v1/launch.py coding --project /project --auth /existing/auth.json --network --resume-session SESSION_ID
```

读取已完成记录不会重跑模型；新目标重新调用 coding。当前入口支持模型、`-c`配置、原隔离/权限选项和输出文件，
自动加入JSON事件及交接schema，不接受另选cwd/worktree/临时会话或自定义最终schema。实现复用
[官方非交互JSON与指定会话续作接口](https://learn.chatgpt.com/docs/non-interactive-mode)，不提供任意宿主命令服务。

## 固定底座与受控入口

固定 wheel/uv.lock 负责可重现，安装身份负责核对内容，Linux bwrap 负责限制指定进程写入；
三者不能相互替代。入口保护它启动的 coding/Runtime 及工具子进程，不接管已存在的全权限桌面会话。
用户日常叫法不决定内部类型：先理解目标、一次使用还是复用产物，再查实际合同/目录选择；明确接口、
manifest或成果格式要保留，只有影响成果/范围的歧义才问。代码长短、步骤数都不是分类标准。

宿主从选定发行 wheel 及固定、带 hash 的运行依赖清单准备**新目录**，不采纳当前可写 venv 的摘要：

```bash
robot-rsi host prepare --directory /path/host/core-0.4.0-v1 \
  --core-wheel /artifacts/robot_rsi_harness-0.4.0-py3-none-any.whl \
  --core-sha256 APPROVED_SHA256 --requirements /artifacts/dependencies.lock \
  --wheel /project/dist/local_extension.whl --codex /existing/codex
/path/host/core-0.4.0-v1/launch.py run --project /project -- guide agent
```

SHA来自已选择的发行记录。依赖清单不含core/本地包，不含editable，必须有完整版本与hash；宿主用uv
离线固定安装、检查所选core内容/依赖兼容，再生成approved.json和独立标准库launch.py。
不接受“把现有安装重新记成基线”。真实启动在只读挂载后重新核对所有安装文件（含bytecode）、工具与
Python可执行文件，再导入core；不使用进程缓存摘要代替核对，不在每次工具调用重复扫描。

宿主目录须在可写项目之外。安装、基线、工具、解释器树与必要祖先只读；当前项目及本用途状态可写。
项目.venv在该命名空间中只读映射到固定安装，保留隔离外原目录。运行CLI采用隔离Python导入，排除cwd、
PYTHONPATH和user-site的同名替换。发现不连接设备；配置不是硬件权限。失败直接返回，不降级全权限。
源码/测试/资料可正常编辑；候选可用单独的项目构建环境，或宿主已准备的构建依赖，运行中不装包。

受控coding使用既有Codex CLI（宿主须先提供现成登录文件）：

```bash
/path/host/core-0.4.0-v1/launch.py coding --project /project \
  --auth /existing/auth.json --network -- \
  --ignore-user-config --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check --model gpt-6.1-sol -c 'model_reasoning_effort="low"' '用户目标'
```

该参数仅用于外层已建立bwrap边界的入口，CLI自身不再嵌套沙箱/审批；[官方参数说明](https://learn.chatgpt.com/docs/cli/reference)
也把该模式限定在外部隔离环境。保护来自实测OS挂载，不来自此参数。认证只读引用，HOME/客户端日志在项目
本用途目录，无全局配置修改；network显式保留联网以使用现有模型，未提供设备/旧服务访问授权。
`launch.py exec --project /project -- COMMAND ARGS`可在同一边界运行现有编辑/构建/测试命令，不能作为隔离外特权命令工具。

普通本地更新在已有授权下由宿主执行，无额外固定审批；受控Agent只提交候选：

```bash
robot-rsi host update --baseline /path/host/core-0.4.0-v1/approved.json \
  --directory /path/host/core-0.4.0-v2 --wheel /project/dist/local_extension_new.whl
```

update保留原core wheel/hash与运行依赖，创建新安装，不覆盖旧安装/技能/任务引用；不兼容依赖或核心覆盖/
启动钩子明确拒绝。新增依赖/底座升级由宿主显式选择新固定产物后prepare，不能靠模型便签或改项目锁文件放宽认可值。
在任务边界切换新入口，原未知/取消效果不重放。当前桌面会话只有通过这个入口另启受控进程才获得上述边界；
不声称已保护隔离外进程、root或另一全权限宿主。现仅支持已验证的Linux路径，不部署服务或更改全局权限。

保留已有项目、适配包和状态目录。宿主 profile.project 指定项目 ID、根目录、本次用途、共同事实键
与明确的资料索引。用途只路由资料，保留原Store字段映射（不是Agent身份）：onboarding/improve → development，run → runtime，
evaluate → evaluation。各类资料可在同一项目中保存，原 Store 的角色/站点/工作区隔离继续有效。

```json
{"id":"my_project","root":"/absolute/existing/project","purpose":"run","common_key":"facts",
 "materials":[
   {"key":"facts","title":"共同约束","path":"facts.md","purposes":["onboarding","run","improve","evaluate"]},
   {"key":"method","title":"运行方法","path":"method.md","purposes":["run","improve"]},
   {"key":"notes","title":"局部开发记录","path":"notes.md","purposes":["improve"]}]}
```

这是 profile.project 的内容。path 是项目内普通相对文件，禁止穿越和符号链接。common_key 指向本次
用途可见的完整必要事实；不要写入开发假设或评估答案。共同事实在进程入口读取，更新后在任务边界重启。
`robot-rsi --profile profile.json project --purpose run` 显示安装/技能/绑定版本和入口，purpose 与原 role
不符会拒绝。模型工具不能切换用途或传入任意根路径。

`workspace_read` 和 `task_context` 含相同短入口；`project_read(query=...)` 最多给三张卡，
`project_read(key=...)` 才读正文，不递归扫描项目。原备注、检查点与经验继续使用；只按有效变化更新、
合并重复和归档，不每轮总结。`task_context(blocks=true, delivered=...)` 沿原变化游标送达。

## 处理位置

- 参数、资料、绑定错误：修正输入或选择，只检查影响当前任务的变化。
- 缺 provider、本地算法/技能不足：在本地项目或共享扩展包修正，增加不可变候选版本。
- 需要改变公共 schema、执行/版本/记忆语义，或可复现核心违约：带最小复现交框架维护方。

Agent 说明判断依据，框架保存证据，不自动诊断。普通本地修正直接推进，不增加审批；公共缺口不靠私改
已安装 Runtime 绕过。`issue_create` 输入 route、rationale、component（安装 distribution 名）、expected、
actual、reproducer，可带 source_task、operation_ids、evidence_refs。route 为 local_input / local_extension /
framework_contract / framework_core。宿主补实际安装、能力、技能和绑定版本，操作必须属于 source_task。

候选沿原 Registry/evaluation/release：固定正常与缺口输入、判据和成本，在独立评估上下文/原evaluation范围比较
baseline_independent / baseline_assisted / candidate_independent。纯计算物理次数为 0，没有适用援助时
assisted 组也如实记录零援助/模型调用。评价实际结果，operation 成功不代表目标成功。原 Releases
通过后才切换；任务固定版本，失败/未知/取消语义保留。非抓放失败结束旧任务，以关联新任务验证。

## 框架问题与答复文件

运行工具可 issue_create 和 issue_get，不能导入维护者答复、安装或标记解决。宿主离线文件交换：

```bash
robot-rsi --profile profile.json feedback export ISSUE_ID --output issue.json
robot-rsi --profile profile.json feedback import-reply reply.json
robot-rsi --profile profile.json feedback status ISSUE_ID
```

答复 JSON 必填 id、issue_id、project_id、component、handling（project/extension/framework）、
disposition（needs_information/declined/answered/update_available）、affected_versions（如 `>=0.1,<0.2`）、
summary、details、migration、validation、provenance（maintainer_file 或 simulated_fixture）。升级需 target_version；
可选 contracts/providers/skills 限定依赖。ID 关联项目/component，错配拒绝；相同 ID 不可变，新答复用新 ID。
文件只作数据，不自动执行。

状态区分 pending、needs_information、version_not_applicable、unrelated_dependencies、update_available、
component_not_installed、installed_needs_validation。安装版本在进程实查。workspace/task 默认最多三条
相关短状态，按 ID 看原答复和迁移/验证动作。无关通知不使全部绑定失效。收到说明不代表已安装。

默认短状态优先显示可行动变化，旁边 framework_update_index 给 total/update_count/pending_count 和
issue_get 分类入口。`issue_get(kind="all|updates|pending", limit=3, cursor=...)` 可继续发现全部记录；
返回 total、counts 和 next_cursor，后者为空表示这一页已结束。cursor 沿原 Store 记录序号固定分页边界，
同一 kind 继续读取时新加入的问题/答复不挤动原页；从无 cursor 再读可看到后来新增项。相关依赖变化和已有
验证状态按实际事实显示，不另建通知队列。宿主也可 `feedback status --kind pending --cursor CURSOR`。

实际安装目标版本且任务/操作成功后，宿主可记录选定验证：

```bash
robot-rsi --profile profile.json feedback attest REPLY_ID --task-id TASK_ID --operation-id OP_ID --note '验证范围与结果'
```

installed_selected_validation 仅表示该安装、所选依赖和明确任务有成功证据，不自动宣告问题解决。
安装或相关依赖/技能变化需重新验证；没有后台网络、通知订阅或新审批队列。

## 独立运行完整例子

从产品源码构建两个 wheel，在新环境安装（不用 editable）：

```bash
uv build --wheel --out-dir /tmp/project-wheels
uv build --wheel examples/project_adapter --out-dir /tmp/project-wheels
uv venv /tmp/project-demo-env
uv pip install --python /tmp/project-demo-env/bin/python /tmp/project-wheels/*.whl
/tmp/project-demo-env/bin/python examples/project_adapter/improve_demo.py --root /tmp/project-improvement
```

使用新 root。示例生成共同/运行/开发/评估资料和原三个 scope 的 profile。首任务保留 offset 缺口，
实际复制修改本地 skill.py，对好/坏候选各做三组两输入评估，沿原 evaluation/Releases 拒绝或发布，
最后启动新进程复用并归档。feedback CLI 实际导出问题，导入相关/待资料/版本不匹配/无关夹具，
用当前固定安装的位置计算记录选定验证。99.0.0 仅为未来版本通知夹具，未安装；位置验证不证明 covariance 已实现。

root 下主要证据为 selected-failure.json、good/bad-report.json、good/bad-evaluation.json、releases.json、
reuse.json、issue-outbox.json、reply-*.json、import-*.json、attestation.json、feedback-status.json。
frozen.json 与 reuse.json 的 installation 应相同。也可先 `--phase baseline` 查看真实失败，自行在
root/good 和 root/bad 建候选，再 `--phase compare`，最后新进程 `--phase reuse`。

这是 authored 离线项目和预设补丁的确定性协议回放；执行的是本地代码变化，核心安装保持冻结。
不调用模型，不证明原生 Agent 自主诊断/改进、未知项目接入或现场效果。

运行入口只收到 position_m/offset_mm 与操作要求，实际执行前 task_context 保存在每行的
context_before_execution；目标和结束摘要不拼入 oracle。固定 expected/容差只在宿主 scored_task 比较，
记录仅公开输入、实际输出和通过/失败结论；真实输出等于预期值是合法观测。selected-failure.json 沿同一边界导出。

### 可复用方法交付

先判断用户要一次结果还是可重复调用的方法。后者要提供真实 Registry 代码入口、固定版本/有效输入例、完整目标判据及两组输入的实际端到端报告；保留必要剩余项。用已有 TaskMemory 或 coding_delivery 可选 delivery，不复制成第二套任务状态。普通修复继续最小 C10 收尾。父进程只读生成的注册/运行报告，不执行用户插件或追加复测。长期流程写在代码中，模型只处理缺口；可沿宿主已授工具查找成熟实现，记录关键来源/版本/采用理由。

完整离线例见 examples/multipoint_inspection（独立安装方法）和 examples/inspection_foundation（仅基础适配/合成图像）。图像判据独立于命令回执，异常报告可完成，确认正常必须通过正常判据；物理未知效果仍不得重放。

同一结构可用于抓放（判据覆盖实际抬升/保持/放置观测）或魔方（观测初态、求解/动作、重新观测最终复原）。命令/求解完成不替代领域结果；本包没有实现真机抓取或魔方流程。

### 同一Agent自动安装候选并继续

瞬时采样先用方法内恢复，输入/配置/现场条件修对应条件，确有方法缺口才改源码。正常流程仍在代码或专用模型中，Agent处理目标、选择和缺口；不固定换Agent或老师。

受控 coding 中，Agent在本项目构建/必要验证wheel，返回中间交接 `candidate_install={wheels:[{path,sha256}],reason,tasks:[{profile,task_id}]}`，paths/validation/checkpoint引用实际源码和已有验证。tasks列当前受影响任务，先结束/归档或交接，不能有活动操作。尚未安装时无需冒称最终方法就绪或提交未完成功能。当地父进程是程序：自动调用原host update，从选定wheel以uv解析兼容新增依赖并固定哈希，保留core与底座约束、旧安装/任务，随后向同一个native session返回新入口/版本继续调用与最终收尾。正常本地安装不交给人或另一个Agent。

只处理项目内选定wheel，不给任意宿主shell，不运行项目插件/动作或源码构建；已有依赖/缓存优先，网络按原宿主选项。固定底座冲突不静默升级，缺凭据/网络或不可用的二进制依赖报告具体条件。新代码用带旧任务来源的新task_id验证，旧未知/取消不重放。无候选零安装/额外模型；相同候选不自动循环尝试，失败保留安装目录/待办并交回同一Agent修正本地代码/依赖元数据；只有真实凭据、网络、设备或宿主授权缺项才用blocked_reason留下明确待办。安装接续是继续工作，不是额外评审，普通C10缺项反馈仍最多一次。

显式中断后沿同一session续作，可使用记录中的新固定入口；原入口也可路由到该项目已认可的候选。只读/source-only、未连接此父进程的MCP/桌面入口仅拥有宿主实际给出的能力，不承诺自动获得开发或安装权限。
