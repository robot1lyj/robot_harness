# 巡检基础离线适配

独立包 robot-rsi-inspection-foundation 0.1.0，复用 synthetic_adapter 0.3.3 的机器人/运动学/单 owner 生命周期，固定 Harness 0.4.0。factory=inspection_foundation，model_ref=fixture_model/v1，机器人 bench，arm bridge_3，关节 carriage_x/carriage_y（0..1）和 turntable（±π）；相机 inspection。

仅提供基础控制、状态和采样，没有完整巡检技能。相机按当前位置 carriage_x 的四位小数字符串查 scenes，normal 生成绿图、anomaly 红图、unreadable 黑图，默认 normal；transient_dark_frames 指每点最初不可读帧数。所有数据明确 synthetic，无真实硬件/视觉准确率承诺。采集时间未知，禁止据此声称物理新鲜度。disconnect_after_submit 用于未知效果边界验证。

依赖 Pillow 的图像生成和既有 synthetic_adapter 的 Cartesian/yaw 示例，不增加 SDK、ROS 或模型。
