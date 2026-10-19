# 同一个项目中运行、改进与反馈

Keep one usage directory per robot for source, local extension development/tests, installed
uv environment, profiles and state. Harness development/release is shared. Environment profiles
and development/runtime/evaluation scopes express purposes inside this project; no extra generic
validation project is required. The [locked offline example](../examples/robot_environment/README.md)
includes a runnable extracted method, saved bindings, same-project scope separation and normal
sync/restart. Environment facts arrive automatically; onboarding is only needed for relevant
inspection/integration work. Source-only inspection stays read-only; explicit implementation
uses only development tools already authorized by its host.

For a first source inspection, use `robot-rsi agent run --project PATH "这是我的机器人项目，你看看"`.
No facts/material index or adapter profile is required. The existing explicit project configuration
below remains available for configured workflows; see [first inspection](integration.md#first-inspection-from-an-installed-environment).

保留已有项目、适配包和状态目录。宿主 profile.project 指定项目 ID、根目录、本次用途、共同事实键
与明确的资料索引。用途只路由资料，沿用原身份：onboarding/improve → development，run → runtime，
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

候选沿原 Registry/evaluation/release：固定正常与缺口输入、判据和成本，独立身份比较
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
用已存在的 0.1.0 位置计算记录选定验证。未来 0.2.0 仅为通知夹具，未安装；位置验证不证明 covariance 已实现。

root 下主要证据为 selected-failure.json、good/bad-report.json、good/bad-evaluation.json、releases.json、
reuse.json、issue-outbox.json、reply-*.json、import-*.json、attestation.json、feedback-status.json。
frozen.json 与 reuse.json 的 installation 应相同。也可先 `--phase baseline` 查看真实失败，自行在
root/good 和 root/bad 建候选，再 `--phase compare`，最后新进程 `--phase reuse`。

这是 authored 离线项目和预设补丁的确定性协议回放；执行的是本地代码变化，核心安装保持冻结。
不调用模型，不证明原生 Agent 自主诊断/改进、未知项目接入或现场效果。

运行入口只收到 position_m/offset_mm 与操作要求，实际执行前 task_context 保存在每行的
context_before_execution；目标和结束摘要不拼入 oracle。固定 expected/容差只在宿主 scored_task 比较，
记录仅公开输入、实际输出和通过/失败结论；真实输出等于预期值是合法观测。selected-failure.json 沿同一边界导出。
