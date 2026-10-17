# Python 后端：Backboard API 连通测试

这个最小后端使用 FastAPI 和官方 `backboard-sdk==1.5.19`。
它提供一次模型调用的测试入口，返回回复、耗时、token 用量及会话编号。
当前功能仅测试 API；酒店搜索、电话确认和邮件流程留给后续开发。

## 1. 安装

需要 Python 3.10 或更新版本。在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
```

运行服务只需要 `backend/requirements.txt`；开发依赖额外安装 pytest。

## 2. 配置密钥

如果还没有本地配置文件，复制模板：

```bash
cp -n backend/.env.example backend/.env
```

用编辑器打开 `backend/.env`，填写：

```dotenv
BACKBOARD_API_KEY=在这里填写你的真实密钥
BACKBOARD_LLM_PROVIDER=
BACKBOARD_MODEL_NAME=
BACKBOARD_TIMEOUT_SECONDS=30
```

密钥只保存在后端。`.env` 和虚拟环境已被 `.gitignore` 排除；不要把密钥写进
前端、截图、提交或聊天内容。环境变量优先于 `.env`，修改 `.env` 后下次请求即可生效。

`BACKBOARD_LLM_PROVIDER` 和 `BACKBOARD_MODEL_NAME` 可以同时留空，使用 Backboard
默认配置；自定义时必须成对填写，从控制台选择有权限使用的 provider/model。
超时时间必须大于 0、最多 120 秒。创建 API key 和兑换活动额度是两个步骤，
请另外确认活动码 `TECHTO7327` 的额度已到账。

## 3. 启动并测试

在仓库根目录、已激活虚拟环境的终端运行：

```bash
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

打开 <http://127.0.0.1:8000/docs>：

1. 展开 `GET /health`，点击 **Try it out → Execute**。
2. `ok: true` 表示服务在运行，`api_key_configured: true` 仅表示已填写密钥。
3. 展开 `POST /api/test`，点击 **Try it out**，保留默认短提示词，再点击 **Execute**。
4. 返回 `ok: true` 和模型回复，才表示这次真实调用成功。

也可以用终端调用：

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/api/test \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Reply with exactly: Backboard API connection successful."}'
```

成功结果包含：

```json
{
  "ok": true,
  "reply": "Backboard API connection successful.",
  "status": "COMPLETED",
  "elapsed_ms": 1250.0,
  "thread_id": "server-returned-id",
  "assistant_id": "server-returned-id",
  "model_provider": "server-returned-provider",
  "model_name": "server-returned-model",
  "usage": {
    "input_tokens": 12,
    "output_tokens": 7,
    "total_tokens": 19,
    "cost_usd": null
  }
}
```

以上数字只是格式示例。API 没有返回的用量或费用字段为 `null`，不会把未知费用当作 0。
每次 `POST /api/test` 都会发起真实模型调用、可能消耗额度，并由 Backboard 自动创建
assistant/thread；测试没有启用搜索、长期记忆或外部工具。`GET /health` 不调用模型。

## 4. 常见错误

错误使用 `{"detail":{"code":"...","message":"..."}}` 格式，服务器不会回传密钥
或原始 SDK 错误内容。

| HTTP 状态 | code | 处理方式 |
|---|---|---|
| 503 | `missing_api_key` | 在 `backend/.env` 填写密钥后重试 |
| 503 | `invalid_configuration` | 检查 provider/model 是否成对、超时是否有效 |
| 502 | `backboard_auth_error` | Backboard 返回 401/403；检查密钥和工作区 |
| 502 | `backboard_credits_error` | Backboard 返回 402；检查账户余额和活动额度 |
| 429 | `backboard_rate_limited` | 稍后重试，避免快速重复请求 |
| 504 | `backboard_timeout` | 稍后重试，或适当提高超时 |
| 502 | `invalid_backboard_response` | 没有取得已完成的文本回复，检查控制台后重试 |
| 502 | `backboard_error` | 其他上游或连接错误，检查网络和 Backboard 状态 |
| 422 | FastAPI validation error | 提示词需要 1–2000 个字符，只接受 `prompt` 字段 |

这是供本机开发的接口，没有应用层登录或公开服务的访问控制；启动命令只绑定
`127.0.0.1`。需要给前端接入或部署时，再确定跨域与访问控制配置。

## 5. 离线测试

```bash
python -m pytest backend/tests -q
```

测试使用模拟客户端，不需要真实密钥，也不会消耗账户额度。

## 官方参考

- <https://docs.backboard.io/quickstart>
- <https://docs.backboard.io/sdk/first-message>
- <https://pypi.org/project/backboard-sdk/>
