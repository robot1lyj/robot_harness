# 离线使用环境的来源映射

这是明确作者构造的接入例子，不是未知项目测试。运行不默认加载这张开发分析表。

| 来源 | 类别与合同 | 处理和产物 | 限制 |
|---|---|---|---|
| legacy_lab.geometry.tip_mm | 计算实现，kinematics.fk v1 | 复用选定公式，薄适配毫米→米/帧，lab_adapter.Geometry | 有界滑台，无IK/碰撞算法 |
| legacy_lab.capture.read_recording | 输入实现，camera.describe/capture v1 | 保留采样时间和预览字节，RecordedCamera | 录制，不是实时采集 |
| recordings.json | 数据及资产来源，CameraObservation/CaptureTime/模型/标定 | 随包保留detail/survey录制与命名资产引用 | 合成/名义数据，不是物理标定 |
| operator_app.inspect_height | UI耦合的稳定任务方法 | 提取序列/区间判据为lab_inspect_height 1.0.0；依赖相机/FK，由task_run返回结果与证据 | 缺能力或坏区间明确失败 |
| operator_app.align_and_move | 未完成的运动流程 | 保留缺失，不伪造技能/API | 无运动owner或设备授权 |
| __main__/ui.show | 应用/UI | 不打包旧入口 | 环境不依赖旧应用启动 |
| Profile/保存的选择 | 环境配置，原Store/BindingSelection | 两个scope共享wheel、隔离笔记/任务/绑定 | 无新增状态服务 |

方法的区间结果不是物理运动成功；精确安装/调用见[本例说明](README.md)。
