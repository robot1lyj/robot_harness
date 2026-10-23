# 固定安装的离线使用环境

本例在一个使用目录内固定Harness 0.4.0与lab_adapter 0.4.3，提取检查方法并保存绑定，
经原task_run执行且跨进程复用。不是未知项目盲测或硬件就绪证明。
已有使用环境只更新所需声明/产物，不覆盖配置、锁、资产或状态；下面仅用于新的示例目录。

在共享产品仓库构建并复制选定文件：

```bash
USE_DIR=/tmp/slider-use
mkdir -p "$USE_DIR/wheels"
cp examples/robot_environment/{README.md,pyproject.toml,uv.lock,manage.py,integration-map.md} "$USE_DIR/"
uv build --wheel --out-dir "$USE_DIR/wheels"
uv build --wheel examples/project_adapter --out-dir "$USE_DIR/wheels"
cd "$USE_DIR"
uv sync --locked --no-dev --no-editable
uv run --no-sync python manage.py prepare
uv run --no-sync python manage.py run --task next
```

prepare是显式宿主配置：发现并启用已装方法，校验只读/计算候选，保存固定资产、绑定与短笔记；
两份profile使用原Store隔离。不安装包、不启动旧应用、不连硬件；新状态运行一次，旧绑定版本不覆盖。
运行模块来自site-packages固定wheel，无旧源码PYTHONPATH/editable。

已配置环境可沿已有Codex客户端与认证进行普通目标：

```bash
.venv/bin/robot-rsi --profile a.json agent run "请检查离线滑台在0.3米位置的工具高度是否位于0.19到0.21米之间，并保存结果。"
```

宿主先提供环境事实，Agent发现代码方法、task_run并中文报告。
录制保留原时间，名义公式/资产不是现场标定、实时就绪、IK或运动支持。
within_bounds=false可以是有效完成的检查结论，与坏输入或缺能力分清。

```bash
uv run --no-sync python manage.py run --environment b --task other --lower 0.24 --upper 0.26
uv run --no-sync python manage.py run --task bad --lower 0.3 --upper 0.2
uv run --no-sync robot-rsi adapter list
uv run --no-sync robot-rsi --profile a.json call skills_list '{"query":"高度"}'
```

这些命令显式选定方法、复用绑定、完成归档，是协议练习，不是自主选择证明。
坏区间产生失败任务而不改写过去的成功。
更新时选新wheel、改声明及uv lock，在任务边界locked sync；保留profiles/state/assets及旧wheel。
更换wheel内容必须刷新锁，不绕过hash。启动用环境程序或uv run --no-sync，不运行时装包。
SDK/CUDA/CAN属于另行授权部署，本例不需要。
MCP可用项目内程序加--profile a.json mcp，非所有入口必经；协议送达不代表任意客户端已遵循。
