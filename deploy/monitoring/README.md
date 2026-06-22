# Monitoring Stack — Vector + Loki + Prometheus + Grafana

一键启动的可观测性栈，用于采集下游应用（如 `zata_code_template`）的 JSON 日志和 Prometheus 指标。

## 快速启动

```bash
cd deploy/monitoring
cp .env.example .env
# 编辑 .env，填写 DOMAIN
vim .env
docker compose up -d
```

## 包含组件

- **Vector**：采集 Docker 容器 stdout 日志，解析 JSON，推送到 Loki。
- **Loki**：日志存储与查询。
- **Prometheus**：抓取应用 `/metrics` 和 Vector 自身指标。
- **Grafana**：统一面板，自动 provisioning Prometheus + Loki 数据源。

## 访问

- Grafana：`https://grafana.${DOMAIN}`
- Prometheus：`http://localhost:9090`（仅在 monitoring 网络内）
- Loki：`http://loki:3100`（仅在 monitoring 网络内）

## 与下游应用对接

下游应用需要满足：

1. 容器输出 JSON 日志到 stdout。
2. 暴露 `/metrics` 端点。
3. Docker Compose 服务带有标签：
   - `com.docker.compose.project=zata-codes-template`
   - `com.docker.compose.service=backend`

详见 `zata_code_template/docs/guides/observability.md`。
