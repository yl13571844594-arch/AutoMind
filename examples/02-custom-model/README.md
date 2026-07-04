# 02 — 自定义模型 / Custom Model

配置 DeepSeek、本地 Ollama 或任意 OpenAI 兼容中转代理。

## 方式 A：Web 界面（推荐）

打开工作台 → 右上角「🔑 API Keys」：

| 提供商 | 填写 |
|--------|------|
| DeepSeek | API Key（`sk-...`），模型默认 `deepseek-chat` |
| Ollama（本地） | 无需 Key；确保 `ollama serve` 已运行，模型如 `llama3.2` |
| 中转代理 | 底部「自定义 OpenAI 标准接口」：`api_base`（如 `https://api.your-proxy.com/v1`）+ 模型名 + Key |

## 方式 B：环境变量

```bash
# DeepSeek
set DEEPSEEK_API_KEY=sk-xxxx        # Windows
export DEEPSEEK_API_KEY=sk-xxxx     # Linux/macOS

# 中转代理（OpenAI 兼容）
set CUSTOM_API_KEY=sk-xxxx
```

## 方式 C：配置文件（CLI / 库使用）

参见本目录 [config.yaml](config.yaml)：

```bash
automind --config examples/02-custom-model/config.yaml "写一个快速排序"
```

## 验证

设置完成后，「🔑 API Keys」面板中点击「🔌 测试连接」应显示各阶段绿色通过；
顶部模型徽标变为绿色「已连接」。
