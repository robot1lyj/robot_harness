# 从小型旧项目提取能力与方法

本例从 legacy_lab 选取计算、录制数据和已有检查方法，独立打包为 lab_adapter。
旧应用入口/UI不进入运行依赖；固定扩展wheel安装后才能装配，不启动旧服务。

```bash
uv build --wheel --out-dir /tmp/project-wheels
uv build --wheel examples/project_adapter --out-dir /tmp/project-wheels
robot-rsi init /tmp/project-use --core-wheel /tmp/project-wheels/robot_rsi_harness-0.2.0-py3-none-any.whl
cd /tmp/project-use
uv add /tmp/project-wheels/robot_rsi_lab_adapter-0.3.0-py3-none-any.whl
```

本例 profile 令 default_ports=false：两套相机(detail 12×8 / survey 4×3)和FK(250/200mm)候选可发现，
宿主按实际输入选择并绑定固定模型/内参。显式设default_ports=true时保留survey默认端口。
没有运动、IK、实时新鲜度或结果识别。录制时间原样保留，要求实时max_age会拒绝；名义标定与公式
只证明这份离线夹具。

通过 binding_candidates、binding_validate、binding_save、binding_read 和 task_begin 的绑定引用接入。
选择是数据，不是可执行代码；保存须匹配实际校验结果。已有任务只在无操作/未固定绑定前attach_to_task，
以后复用摘要与变化，只校验改变的角色，旧任务/未决操作保持，不授权重放。

lab_inspect_height 是可复用代码方法，依赖相机和FK，返回区间判定与证据；不是单纯拟建包名。
完整固定安装/执行见[使用环境例子](../robot_environment/README.md)。
局部改进、三模式评估、发布与新进程复用见[项目工作流](../../docs/project-workflow.md)。
improve_demo.py 在新root做已说明的字面补丁；框架代码不变，维护者答复为模拟文件，
该确定性协议不冒充原生模型或实际公共发布。
