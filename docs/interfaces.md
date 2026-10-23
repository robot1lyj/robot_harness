# 对象与能力接口

## 已安装合同与字段映射

通过 harness_read(guide="interfaces") 分页读取本指南，tool_describe(name=...) 查询实际输入 schema。
这些只读入口描述公共合同，不证明当前机器人有实现。以下概述 v1；精确字段/验证由安装代码拥有。

| 合同/工具 | 需要核对的事实 |
|---|---|
| kinematics.fk / FKRequest | model_ref={id,version}、arm、命名 joints、base_frame、target_frame。关节/限位依所选 RobotModel；移动关节用米、转动用弧度。计算不是采样状态或运动 |
| FKResult / Transform | model_ref、pose={target_frame,source_frame,matrix}、source、qualification；T_base_target 为刚性4×4变换，平移用米，末行为[0,0,0,1]。说明帧方向与单位换算 |
| RobotModel | reference、resource_id、joints(id/kind/lower/upper/tolerance)、manipulators、frames、parameters、source、qualification；资产有固定 id/version 与任务引用 |
| camera.capture / CameraObservation | id、camera、source、qualification、received_at 与图像帧；ImageFrame 有 stream_id、artifact_id、尺寸/编码。合格预览可缺 frame_id/capture_time/intrinsics；图像本身缺失是来源缺口 |
| CaptureTime | value_s、kind(unix/device_monotonic)、clock_domain、可选 uncertainty_s。保留采集来源，接收时间不能顶替未知采集时间；同步/精定位另需适用证据 |
| robot.state / RobotState | id、resource_id、model_ref、命名 joints、可选 tools、capture_time、received_at、boot_id、epoch、source、qualification。计算或SDK函数名不能制造实测反馈/重启身份 |
| robot.command | 原操作 owner 提交，区分 accepted 回执与采样完成反馈，取消/停止及未知效果沿原操作语义 |
| task_run / task_finish | request_id、skill、parameters、当前任务/epoch；skill_describe 给所选技能合同。用真实 operation/evidence 完成完整目标后归档；简单检查不必另建通用 provider |

来源映射区分直接字段、可计算转换、来源/设备缺项及未支持的公共语义；差距归具体适配/方法、资产、
来源owner或框架修正，不能编字段让来源看似满足合同。

## 环境与项目事实

station/namespace 与原 role/Store 继续隔离；项目 ID/purpose 是另一维度，不迁移作用域或增加数据库。
task_begin、workspace_read、task_context 共享配置态环境投影，提供安装身份、有限目录卡/计数、
详情入口、任务固定资产/技能和绑定。完整说明与实时观测仍按原工具读取。
超大上下文保留身份/用途/资格及完整入口，不声称全部约束已送达。

MCP initialize 给配置身份/用途/资格，不构造另一个 Runtime、不隐式连接。
source-only mcp --project 与 agent run --project 共用 helper，--profile 与 --project 冲突，
缺来源根返回 project_not_found。init/guide 是宿主CLI，不扩大运行模型的文件/设备权限。

本文面向适配器作者。先按职责分类对象，再选择所需端口。Scene、Manipulation、OutcomeVerifier、CameraPort、DepthPort、统一适配工厂、命名设备命令、KinematicsPort 与模型/标定读取已实现并有离线 synthetic 样例。下列其他对象是设计覆盖示例，不表示已有对应实现；精确字段与工具 schema 以安装版本为准。

## 放在哪里

| 你要接入或描述的对象 | 归类与使用方式 |
|---|---|
| 机械臂、移动底盘、夹爪、吸盘、相机、力传感器、可控工装 | 设备资源；按实际提供的能力装配，多个资源可以共享 owner |
| 零件、容器、台面、障碍、目标区域 | 场景实体；位置和状态来自相应观测，普通物体不需要设备驱动 |
| 机器人几何、工具参数、标定、地图、模型权重 | 版本化数据资产；由配置引用，供实现读取 |
| 检测、FK/IK、规划、策略推理、验证算法 | 能力实现；声明输入输出及实际效果，按需使用资产或观测 |
| 抓放、巡检、标定采样、记忆整理、接入工作方法 | 技能；在需要可复用多步骤方法时创建，单个算法函数通常直接作为能力 |
| 图像、点云、关节快照、预测、执行回执、验证报告 | 观测与证据；保留来源和时间，大数据按引用读取 |
| 当前目标/检查点、适用经验 | 任务状态与记忆；组织运行资料，不替代实时观测或资产 |

能力描述“能做什么”，实现包交付“由什么实现”，工具提供“Agent 如何调用”。一个包可同时交付 provider、技能与数据；一个 provider 可支持多个能力。工作区组织这些对象，profile 绑定实例、版本和位置；无需每类对象各启动服务。

型号定义、现场实例和当前状态分别管理。例如模型规定关节结构，机械臂实例提供设备身份，本次采样提供关节角；更换模型与刷新状态是不同操作。

## 接入对象

工作区按需组合机器人、相机和计算能力。末端相机也是独立的相机资源，安装关系由坐标系与标定引用表达。接口并列不要求各启一个进程：已有设备管理器可以同时提供机器人和相机端口。

| 接口 | 提供什么 | 必需条件 |
|---|---|---|
| 资源描述 | 资源 ID、类型、角色、实现版本、支持的能力、坐标系/模型引用与连接状态 | 提供该资源时 |
| 机器人设备端口 | 静态描述、显式连接/关闭、实测状态、停止/保持结果 | 需要机器人时 |
| 机械臂动作端口 | 命名关节轨迹、声明支持的末端命令及执行回执 | 宣称对应动作能力时 |
| CameraPort | 描述相机、采集帧引用与时间、内参、双目配对信息 | 需要相机时 |
| DepthPort | 从合格双目观测得到米制深度 | 需要深度时 |
| KinematicsPort | 指定模型下的 FK/IK 纯计算 | 需要运动学时 |
| Scene / Manipulation / OutcomeVerifier | 场景观测、任务规划/分段执行、结果验证 | 按技能要求提供 |

具备关节运动接口不等于具备抓放技能所需的物体定位、路径规划和结果验证。缺少可选能力只影响依赖它的任务；纯计算任务无需配置假机器人。

相机、力传感器都属于传感资源，各自的数据合同仍保留类型与单位。底盘运动、灵巧手、力控和可控工装尚无通用已实现端口；需要时按其语义增加合同，不通过套用机械臂命令来宣称支持。

## 能力与 Agent 视图

原生会话的 tools_list/tool_describe 标明 tool_scope=native_agent：available 沿原含义表示底层能力绑定，
callable 表示该客户端实际暴露此工具，两者不替代执行准入。原生入口提供既有 resource_connect 和 robot_state，
仍经同一 Runtime/会话身份/资源 owner；目录发现不连接，也不授权设备。同 Runtime 已连接的资源可直接复用。

按行为区分描述/观测、感知/估计、计算/规划、执行/控制和验证/记录。每项能力说明真实效果：例如策略推理可以只输出动作建议，由执行端口应用；测量流程也可能需要移动探针，必须把执行步骤表达出来。

Agent 首先读取当前目标、必要约束与相关技能/能力短说明；选中后再取对应合同、资源和资产引用；运行中接收关键结果与变化。原图、曲线、模型正文及历史按需读取。高频传感和控制留在宿主通道，沿用已有操作等待与结果引用；不要求模型每帧介入。

新增设备通常只需适配包和配置；新增算法优先实现已有能力；语义不同才增加合同；形成可复用方法才增加技能。普通现场参数放配置和资产。这个选择顺序也是接入基础技能的工作方式。

[面板巡检示例](task-walkthrough.md)把这一流程串起来：操作是否完成、读到的业务结论、用户目标是否达成分别表达。已观察到异常可以是成功巡检的结果；识别不足是否阻碍完成取决于原任务要求。领域结果不借用未知物理效果的错误标签。

## 装配入口

接入扩展使用已安装 Python 包的 `robot_rsi.adapters` entry point。宿主配置选择一个工厂及其配置；工厂返回资源描述、执行 owner 与 SkillBindings，可组合其他包提供的端口。共用技能和 MCP 工具不需要按设备型号改写。

现有项目可由接入 Agent 梳理为上述产物；源码功能先映射公共合同并验证。Agent 在宿主提供的已安装候选中选择，由 Harness 解析、校验和保存。已有适用绑定直接复用；绑定记录表达角色到资源/provider、资产版本、适用条件和证据，不复制技能或创造底层能力。

首版选择覆盖 camera/kinematics 两种类型化端口，功能试用只读/计算；发现与静态检查不连接硬件。保存新版本供后续任务选择，已有任务固定所用版本；所选依赖变化时只复验受影响项。具体使用方式见[接入指南](integration.md)。

入口发现、静态描述和配置检查不连接硬件。连接在明确的运行生命周期执行；实际设备写入与相机采集各有一个 owner。网络地址、SDK 参数、进程位置与模型路径由宿主配置决定。

现有代码合同可查阅 [能力接口](../src/robot_rsi/capabilities.py)与[感知接口](../src/robot_rsi/perception.py)。精确字段见 [资源与计算合同](../src/robot_rsi/resources.py)、[工厂接口](../src/robot_rsi/adapters.py)和 [可运行外部样例](../examples/synthetic_adapter/README.md)。

### 已安装工厂与最小装配 API

工厂为 `factory(AdapterContext) -> Adapter`，并以 `factory.config_model = Options` 指定 Pydantic 配置模型（Options 继承 `robot_rsi.contracts.Contract`）。用 `robot-rsi --version` 查看安装版本、`robot-rsi adapter list` 只读列出已安装工厂。标准 entry point 在包的 pyproject.toml 声明；配置检查只加载选定工厂的配置 schema，不构造设备。

```toml
[project.entry-points."robot_rsi.adapters"]
synthetic_fixture = "synthetic_adapter:factory"
```

```json
{"robot":null,"host":{"adapter":{"factory":"synthetic_fixture","options":{"robot":false,"camera":false,"shared_owner":false}},"model_ref":{"id":"fixture_model","version":"v1"}}}
```

`robot` 是保留的内置选择/旧 scope 字段，原值不迁移；新工厂配置可设 null，实际设备是否存在由返回端口和 resources 决定。内置 mock 也经同一 entry point 通路。工厂不能在 import/构造/describe 中连接设备。

AdapterContext 提供 profile、store 和已验证 options。Adapter 返回 `bindings: SkillBindings`、设备 `resources: tuple[Resource,...]`、可选唯一 `owner`、按 owner_id 命名的 `lifecycles`，以及当前需要的 `models`/`calibrations` 固定记录。机器人/相机端口均可为 None；计算实现由 capabilities 和可选端口描述，不伪装成机器人或场景实体。Resource 的 kind 是可扩展标签，不是覆盖全部对象的继承树。

`resource_connect(resource_id)` 明确连接指定 owner；同 owner 的资源共用一次连接，包括并发请求。Adapter 串行处理连接与关闭，关闭等待在途连接结束后清理；失败连接不登记，可重试，关闭后拒绝新连接。Runtime 退出先结束 Operations，再将每个生命周期对象关闭一次。只读 `resources_list`/`capabilities_list` 不连接；connected 来自宿主生命周期记录，不是实时设备探测。长驻 MCP/HTTP 或 `async with Runtime(profile)` 内完成连接和后续调用；单次 CLI 调用退出也会关闭资源。

机器人端口需提供 `model`、`state()`、`submit(command, operation_id)`、`wait(receipt)` 和声明的能力；原执行 owner 提供 validate_arm/validate_observation/hold 与执行资格。`robot_state(resource_id)` 保存带采集时间的 RobotState；`model_get(reference)` 按需读对应关节、工具、范围/单位/容差定义。`robot_command` 仅经原 Operations 调用端口，输入 request_id、observation_id、resource_id、model_ref、arm，以及 trajectory 或 effector 二选一。它是低层诊断入口；受管任务仍以 task_run 代码技能组织动作。新适配器不自动获得旧 robot_move/robot_gripper 的语义。

trajectory 使用递增 time_from_start_s 和命名 positions；effector 使用 tool_id/command/value/unit，范围、单位和确认容差来自模型。CommandReceipt 只表示 accepted/running；CommandFeedback 的 completed 必须带实测状态、来源与相同 command/operation 身份。低层操作成功仍返回 verified=false，不冒充抓放验证或整个目标。失联未知、取消和重启沿用原 Operations；恢复不重发历史动作。

## 单位、时间与动作结果

- 关节、工具、相机使用命名 ID；不约定左右双臂、关节数量或统一夹爪量程。关节类型与顺序来自模型，SDK 单位在适配器转换为 rad / m。
- 位姿包含明确源/目标 frame；`T_target_source` 满足 `p_target = T_target_source @ p_source`。状态和图像保留采集时间、时钟域与来源，不能用请求完成时间冒充采样时间。
- 提交回执、执行中、实测完成、失败和未知效果分别表达。失联时保留未知；恢复任务记录不会重新播放物理动作。
- 相机继续使用 CameraObservation、CaptureTime、Intrinsics 与 StereoCalibration。帧以 artifact 引用传递，模型上下文只取当前所需资料。

## 运动学与标定

FK 输入机器人模型、命名关节位置及目标 link/frame，返回位姿。IK 输入模型、目标位姿、seed 与约束，返回解或无解/未收敛及残差。两者均为计算能力，不读取设备或发运动命令；IK 成功不代表轨迹已检查。

标定分为三部分：工作流安排采样与求解，算法包提供计算，标定产物提供运行参数。内参/畸变和双目参数属于相机及图像流；手眼/固定外参连接坐标系；TCP 校正属于工具与机器人模型。普通任务读取指定版本产物，不每次重新标定。

CalibrationRecord 包含版本、适用资源/模型/坐标系/图像流/分辨率、参数或 artifact 引用、方法、样本来源与误差。原始流与校正流明确区分。末端相机在采集时的位姿可由 `T_base_tool(q(t)) × T_tool_camera` 组合；时间与模型适用性由使用方根据任务要求核对。

优先复用设备已有求解器。独立运动学可选 [Pinocchio](https://github.com/stack-of-tasks/pinocchio)，相机及手眼标定可选 [OpenCV calib3d](https://docs.opencv.org/4.10.0/d9/d0c/group__calib3d.html)；它们是可选适配依赖，不要求所有工作区安装。

## 版本与工具入口

公开的 `kinematics_fk` 使用 model_ref/arm/joints/base_frame/target_frame；`kinematics_ik` 使用 model_ref/arm/target/seed/joint_constraints。FKResult 的 pose 为 T_base_target；IKResult 区分 solved/no_solution/not_converged，残差必须注明来源。工具只计算，不建立运动操作。外部样例只验证已知 Cartesian/yaw 解，不是通用求解器。

`calibration_get` 输入 reference，以及产物要求的 resource_id/model_ref/stream_id/resolution/source_frame/target_frame。返回 applicable/reasons/record；applicable 只表示所提供条件与产物元数据匹配，不表示已连接设备或现场标定通过。缺失或不匹配的适用条件明确失败。CalibrationRecord 复用 Intrinsics/StereoCalibration，固定外参、手眼/TCP 可存 Transform 或原 artifact 引用；不可用 artifact 引用不被当作有效参数。未提供 FK/IK 或相机只影响依赖该能力的调用。

RobotModel 可携带当前所需的命名关节/工具、几何标量 parameters 或已有 artifact_id；solver 消费指定版本的数据，不把 provider 的安装版本当成数据版本。

profile.host.model_ref/calibration_refs 选版本；task_begin 在原检查点 resource_versions 中固定引用。后续配置选新版不会改在途任务；旧版需仍可由该 provider 使用，否则明确失败，不换成新版。固定记录同 ID/版本内容不可覆盖。描述/上下文只带必要引用，`model_get`/`calibration_get` 才读正文；普通任务不采样或重新标定。用 `robot-rsi call tool_describe '{"name":"robot_command"}'` 可查看安装版本的完整 schema。

## 场景选择工具与任务引用

工厂可以在 Adapter.candidates 返回 `Candidate(info=CandidateInfo(...), bindings=SkillBindings(...), probe_tools=(...))`。
CandidateInfo 包含 reference、description、distribution、resource_ids、model_refs、calibration_refs、conditions；
bindings 只含 camera/kinematics，资源必须已在该 Adapter 中，不能另造控制 owner。provider/合同来自实际端口，
安装包身份由宿主读取，模型不能指定 Python 入口。精确字段归 [scene_bindings.py](../src/robot_rsi/scene_bindings.py)。

| 工具 | 输入与结果 |
|---|---|
| binding_candidates | requirements（如 camera.capture:1）、可选 query/limit≤3；返回相关短候选、资源/资产引用和缺项，不采集 |
| binding_read | reference 或 scene_id；无参数列最多 3 个保存摘要，任务内优先读其固定引用。可提供 conditions 检查适用性。返回 valid/changes，不试用 |
| binding_validate | selection + trials，可带 previous 引用复用未变化端口的证据。静态结果与实际试用结果分开；空 trials 只能静态通过，不能据此保存 |
| binding_save | reference + validation_id；只保存该次宿主验证的原选择，ID 必须匹配 scene_id，同版本不可覆盖。attach_to_task 可在已有任务首次执行、首次固定绑定前附着 |

selection 含 scene_id、purpose、reason、roles（camera/kinematics→候选 ID）、requirements、model_ref、
calibration_refs、conditions、skills。trial 含 role/tool/arguments，tool 仅 camera_capture、kinematics_fk 或
kinematics_ik，且必须被该已安装候选允许。宿主调用原 Runtime 工具校验合同、模型/frames、图像 artifact 与
标定的流/分辨率/内参；IK 无解不能当作该输入已通过。结果保留实际来源与 qualification。验证不连接设备、不运动，
若该只读端口需要连接，接入方应在其他已授权生命周期先显式完成；验证工具不会代连。

`task_begin(..., binding_ref={id,version}, binding_conditions={...})` 将引用、model/calibration 版本固定在
resource_versions。并发任务通过各自的 SkillBindings 使用所选端口，进程默认端口不被改写；保存新版仅供新任务。
技能发现、描述、上下文和执行按同一任务绑定判断可用性；目录查询缓存按有效能力身份区分。工厂可以只提供候选，
无需先绑定默认相机/运动学端口；未绑定请求仍使用宿主默认。工作区和站点摘要也读取本任务端口。
task_context 只带固定绑定摘要与相关变化。需要整合新信息时先 binding_read，变化角色才提供新 trials，
previous 复用依赖/条件未变且覆盖所需合同的旧证据；候选说明或无关候选变化不会让该绑定失效。
所选 provider 的实现/配置、资源、资产或安装包改变会报告相关角色。进程内不支持热改安装；安装身份变化仍按原规则
阻止旧任务继续执行。旧结果始终可读，未知操作不重放。

可运行例子见[旧项目接入与跨进程复用](../examples/project_adapter/README.md)。接入分析不是运行记忆，
离线试用不是现场标定、就绪或运动验证；原生 Agent 自主绑定需在实际部署环境单独验证。

## 同项目资料与框架反馈

宿主 profile.project 沿现有角色选择用途；project_read 找最多三项资料或按键读正文。issue_create 保存有依据的处理位置与最小复现，issue_get 读取短状态或关联详情。答复导入、安装核对与选定验证仅宿主 CLI，沿原 Store/任务版本/送达游标；字段、命令与完整独立安装例子见 [项目工作流](project-workflow.md)。

### 可选完整目标交付（core 0.4.0）

TaskBegin / task_checkpoint_save 可带 delivery：kind=one_off 或 reusable_method、original_goal、criteria（id/description/required）、entry（skill/version/parameters）和必要 remaining。目标与必要判据建立后不可被阶段摘要替换；尚未声明可沿 save 增补，入口/剩余项随事实更新。普通旧任务无需声明。

task_finish 可带 criterion_results（criterion_id、passed/failed/unknown、source、evidence_refs、remaining）；技能 result 使用相同结构时，结束自动读取原操作结果，调用者不能覆写。成功要求每个必要判据有带证据的 passed 且无必要剩余项。域内 unknown 不改变操作的物理效果状态。

可复用方法的 method_evidence 引用固定 JSON artifact，含 registration（实际 skill_describe）和 runs（source、实际 operation），至少两组不同 parameters、同一 skill/version，逐项结果满足声明判据。coding Handoff 复用 delivery 与一次结果的 criterion_results，method_evidence 为项目内 JSON 文件路径，父进程只读既有报告结构，不导入插件、不执行动作或复测。结构一致性不能代替报告来源的真实性；证据标明执行来源与适用范围。

SkillContext 新增可选 operation_id 与 artifacts.read(id)/write(bytes,mime,metadata)，仅复用当前 Store 的产物读写；不提供任务/操作状态修改。旧技能上下文构造保持兼容。

受控 coding Handoff 可选 candidate_install：wheels（项目内path/sha256）、reason、tasks（当前受影响任务的profile/task_id）。这是同一Agent的中间安装请求，父进程读原任务边界、调用原host update并记录原coding_delivery.candidate_installs，再同session返回新入口继续原目标；不是新的运行工具/审批或最终完成声明。固定core/底座不变，兼容新增依赖由uv生成哈希锁，原role/Store保持。

安装失败回到同session修正本地候选；不重复安装相同失败产物。Handoff.blocked_reason仅用于真实凭据/网络/设备/宿主授权缺项，保存pending而非成功；不是正常安装所需人工审批。

### 公开技能编写入口（core 0.4.1）

`robot_rsi.authoring` 复用 SkillContext/SkillResult，提供 artifact、criterion、result、request_id；
结果标志显式填写，判据由领域观测决定。`skill create` 模板与
[技能编写短指南](skill-authoring.md)不要求读取 Runtime/Store/coding_host 源码。
`evidence --method SKILL --task-id A --task-id B --output FILE` 从原记录导出注册与调用报告，
可选 --version 固定同一版本；原单任务导出兼容。此入口不自动完成/归档任务或判定方法成功。
