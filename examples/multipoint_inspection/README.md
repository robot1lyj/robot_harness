# 可复用多点视觉巡检

安装 robot-rsi-multipoint-inspection 0.1.0 后，按现有 Registry 发现/启用 multipoint_inspection/0.1.0，再由 skills_list / skill_describe 读取实际合同。一次 task_run 输入 points、resource_id、arm、model_ref，可选 camera、mode=confirm_normal/report、resamples（0..3）。代码按当前位置最近邻排序，连续移动/关键帧检查，局部恢复只补采样。

输入例：`{"points":[{"id":"a","joints":{"carriage_x":0.3,"carriage_y":0.1,"turntable":0}}],"resource_id":"bench","arm":"bridge_3","model_ref":{"id":"fixture_model","version":"v1"}}`。模型/资源来自宿主固定绑定；共享方法不包含设备地址、SDK 或离线场景控制字段。

result.completed/verified 表示过程完成与测得移动符合，报告 artifact 独立给出 report / normal 判据。confirm_normal 需两项为必要判据；report 仅 report 必要，可成功产生异常报告。图像黑暗为 unknown，绿通道占优为 passed，其余 failed；仅是 Pillow 读像素的合成教学规则，不是通用视觉或现场验证。stage 事件保存 confirmed/remaining，取消和未知物理效果沿现有 owner/HOLD/epoch 处理。

方法包独立于基础包；用户自己的兼容能力可复用入口。完整离线调用例 `demo.py`。依赖 Pillow（随核心环境固定），采用标准 Image/ImageStat 读取图像均值，无额外模型或算法依赖。
