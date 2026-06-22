# 监控栈前置知识与背景

> **本文档已归档。** 具体部署与使用说明请参考正式文档：
> [`docs/architecture/monitoring-stack.md`](docs/architecture/monitoring-stack.md)。
>
> 本文保留历史背景信息，供理解最初选型思路时参考。

## 历史选型思路

为 Zata 下游 VPS 部署一套完整的容器化监控栈，需要同时解决三件事：

1. **日志散落**：N 个容器各自的 stdout/stderr，没有聚合入口。
2. **指标缺失**：CPU/内存/磁盘只能 `docker stats` 看实时快照，没有历史曲线。
3. **故障定位难**：错误在日志里，但触发它的是资源指标，缺一个把两者并排看的工具。

## 为什么选 Loki + Prometheus + Grafana

| 方案 | 组成 | 优点 | 缺点 |
|---|---|---|---|
| ELK | Elasticsearch + Logstash + Kibana | 全文索引强、生态成熟 | ES 重（>=4GB RAM）、运维成本高 |
| EFK | Elasticsearch + Fluentd + Kibana | 同上 | 同上 |
| **Loki + Prometheus + Grafana** | Loki + Prometheus + Grafana + Vector | 极轻、标签索引而非全文、复用 Grafana | 全文检索弱、需要 PromQL + LogQL 双语言 |

选 Loki 体系的核心理由：**单 VPS 资源有限**。ES 在 <8GB RAM 的机器上基本跑不动，而 Loki 的"只索引 label、不索引全文"哲学让单进程模式可以在 512MB 内存下扛住日均 GB 级别的日志量。

## 范围

- 覆盖 Logs + Metrics。
- Trace（Jaeger / Tempo）不在本次范围内。

## 下一步

查看正式部署文档：[`docs/architecture/monitoring-stack.md`](docs/architecture/monitoring-stack.md)。
