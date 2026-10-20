# 独立模拟适配包

本包是无硬件SDK的 robot_rsi.adapters 工厂：三命名关节机械结构 bridge_3、以Pa表示的vacuum工具、
可选独立相机与已知 Cartesian/yaw FK/IK。没有场景定位、抓放规划或结果识别；不是通用IK或真机支持。

在共享框架仓库构建固定 wheel，再从新的使用目录安装：

```bash
uv build --wheel --out-dir /tmp/synthetic-wheels
uv build --wheel examples/synthetic_adapter --out-dir /tmp/synthetic-wheels
robot-rsi init /tmp/synthetic-use --core-wheel /tmp/synthetic-wheels/robot_rsi_harness-0.3.0-py3-none-any.whl
cd /tmp/synthetic-use
uv add /tmp/synthetic-wheels/robot_rsi_synthetic_adapter-0.3.0-py3-none-any.whl
```

从产品源码运行 demo.py，显式给 profile 和当前使用项目内的新状态根；Python导入固定安装的实现，
不是旧源码PYTHONPATH。共享/仅机器人/仅相机/纯计算 profile 位于本例 profiles/。
config-check 与目录查询不连接资源。demo 是无模型的显式软件合同调用，不是原生Agent选择证明。

resource_connect 在同一Runtime中显式打开owner，共享资源只connect/close一次。
robot_state 返回命名关节/工具、时钟及模型；model_get 读固定定义。
robot_command 的 accepted 回执与采样完成反馈不同，原始命令不自行宣告完整目标成功。
FK/IK不访问设备；模型/标定v1/v2由配置选择，任务保留初始引用。

可选 fixture_fk 1.0.0 代码技能在 skills/check_fk，按原skill register/enable后由task_run调用。
计算结果不证明物理位姿或碰撞安全。基础端口验证、日常方法与现场验证请分别取证。
详见[接入指南](../../docs/integration.md)及[接口合同](../../docs/interfaces.md)。
