# 北京联合大学 SCOW MCP 服务器

这是一个用于北京联合大学 SCOW 超算平台的本地 MCP 服务器。它通过 SCOW 的 HTTP 登录接口和 WSS WebShell 接口，让 MCP 客户端可以直接调用超算平台终端。

## SCOW 服务器地址

默认服务器地址：

```text
https://scow.buu.edu.cn
```

WebShell 地址格式：

```text
https://scow.buu.edu.cn/shell/{集群}/{登录节点}
```

服务器地址已经作为默认配置写入程序，也可以通过环境变量覆盖：

```bash
export SCOW_BASE_URL=https://scow.buu.edu.cn
export SCOW_CLUSTER=你的集群名称
export SCOW_LOGIN_NODE=你的登录节点地址
```

## 当前进度

- 已实现纯 Python MCP Server。
- 已实现 SCOW HTTP 登录流程。
- 已实现验证码获取和本地保存。
- 已实现登录会话 Cookie 的本地保存。
- 已实现 SCOW WSS WebShell 连接。
- 已实现普通命令执行和交互式输入。
- 已实现终端输出读取和窗口大小调整。
- 已在北京联合大学 SCOW 环境中完成实际连接验证。

## MCP 工具

- `scow_login_start`：获取登录验证码。
- `scow_login`：提交用户名、密码和验证码。
- `scow_status`：查看登录与 WebShell 连接状态。
- `scow_exec`：执行普通 Shell 命令。
- `scow_send`：发送原始终端输入。
- `scow_read`：读取终端输出。
- `scow_resize`：调整终端大小。
- `scow_disconnect`：断开 WebShell 连接。

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r scow_mcp/requirements.txt
```

## 启动 MCP Server

```bash
PYTHONPATH=scow_mcp python scow_mcp/server.py
```

也可以使用启动脚本：

```bash
./scow_mcp/python-mcp.sh
```

## 安全说明

- 密码只在内存中使用，不会写入代码仓库。
- 登录会话保存在本机 `~/.scow-mcp/cookies.json`。
- 验证码图片只保存在本机临时目录。
- 不要提交 Cookie、密码、验证码、Token 或内部测试输出。
- 北京联合大学 SCOW 的部署测试记录保存在对应的私有仓库中。

## 许可证

本项目使用 MIT License。
