# EFK 日志平台详解

> 完成日期：2026-08-14 | 涉及主机：k8s-master、k8s-node1、k8s-node2（Harbor）

---

## 目标

搭建 EFK（Elasticsearch + Filebeat + Kibana）集中式日志平台，实现多节点容器日志的实时采集、Kubernetes 元数据富化（命名空间/Pod 名/标签）、Kibana 检索，以及 ILM 索引生命周期管理（hot → delete 自动清理旧日志）。

---

## 环境

| 主机 | IP | 角色 |
|------|-----|------|
| k8s-master | 192.168.182.164 | control-plane（kubectl 操作入口） |
| k8s-node1 | 192.168.182.168 | worker（ES + Kibana 所在） |
| k8s-node2 | 192.168.182.166 | worker + Harbor（镜像中转） |

命名空间：`logging`

---

## 一、EFK 架构

```
┌─────────────────────────────────────────────────────┐
│  各节点容器日志                                       │
│  /var/log/containers/*.log （kubelet 统一收集）       │
└──────────────────────┬──────────────────────────────┘
                       │ 读取（hostPath 只读挂载）
                       ▼
        ┌──────────────────────────────┐
        │  Filebeat（DaemonSet 每节点一个）│  轻量采集器
        │  解析 + add_kubernetes_metadata │  加 命名空间/Pod/标签
        └──────────────┬───────────────┘
                       │ 写入
                       ▼
        ┌──────────────────────────────┐
        │  Elasticsearch（存储+全文检索） │  倒排索引，秒级搜关键字
        │  索引按天滚动，ILM 自动删旧     │
        └──────────────┬───────────────┘
                       │ 查询
                       ▼
        ┌──────────────────────────────┐
        │  Kibana（可视化检索界面）        │  Discover 搜日志
        └──────────────────────────────┘
```

**为什么是 EFK 不是 ELK？** ELK 里的 L 是 Logstash——JVM 写的重量级日志管道（几百 MB 内存 + 复杂管道配置），用于大流量日志的中间清洗/转换。F 是 Filebeat——Go 写的轻量采集器（几十 MB），只负责「收 + 发」。中小规模直接用 Filebeat 写 ES 足够，不需要 Logstash 这一层。

---

## 二、版本选择：为什么用 ES 7.17 而不是 8.x

| 维度 | ES 7.17 | ES 8.x |
|------|---------|--------|
| 安全 | 默认关闭，开箱即用 | **默认强制开启** TLS + 密码，单机也要配证书 |
| 复杂度 | 低，适合学习 | 高，首次部署要生成 CA、配节点间 TLS、用户认证 |
| 单节点 | 完全够用 | 能单节点，但配置多 |

8.x 的安全特性对新手是纯负担。7.17 是 7.x 最后的长期维护版，最稳。**生产环境新项目上 8.x**，学习项目聚焦「日志链路 + ILM」，7.17 最合适。

---

## 三、部署步骤

### Step 0：资源评估 + 节点定位

ES 很吃内存（JVM 堆 512M + 堆外 + 系统），Kibana 也是 Node.js 应用。部署前先看节点空闲内存，选最空的 worker：

```bash
free -m        # 各节点执行
df -h /        # 看磁盘（ES 索引占空间）
```

本案例：node1 剩 2.4G、node2 剩 1.5G → **ES + Kibana 放 node1**，打标签固定：

```bash
kubectl label node k8s-node1 es-node=true
```

Filebeat 是 DaemonSet 每节点一个，无需挑位置。

### Step 1：镜像中转（docker.elastic.co）

ES/Kibana/Filebeat 镜像来自 `docker.elastic.co`（不是 Docker Hub），在**有代理的 node2** 上拉，然后推 Harbor：

```bash
docker pull docker.elastic.co/elasticsearch/elasticsearch:7.17.20
docker pull docker.elastic.co/kibana/kibana:7.17.20
docker pull docker.elastic.co/beats/filebeat:7.17.20

docker tag docker.elastic.co/elasticsearch/elasticsearch:7.17.20 192.168.182.166/library/elasticsearch:7.17.20
docker tag docker.elastic.co/kibana/kibana:7.17.20           192.168.182.166/library/kibana:7.17.20
docker tag docker.elastic.co/beats/filebeat:7.17.20          192.168.182.166/library/filebeat:7.17.20

docker push 192.168.182.166/library/elasticsearch:7.17.20
docker push 192.168.182.166/library/kibana:7.17.20
docker push 192.168.182.166/library/filebeat:7.17.20
```

### Step 2：ES 的硬性前提 vm.max_map_count

**在 ES 所在节点（node1）执行**：

```bash
sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" >> /etc/sysctl.conf
```

**为什么必须调？** ES 底层用 Lucene，Lucene 用 **mmap（内存映射）** 读写索引文件，每个段的每个部分都要映射成内存区域。Linux 默认 `vm.max_map_count=65530`，很快耗尽，ES 直接拒绝启动并报 `max virtual memory areas vm.max_map_count is too low`。

### Step 3：部署 ES（单节点 + 512M 堆）

关键配置点：

```yaml
env:
- name: ES_JAVA_OPTS
  value: "-Xms512m -Xmx512m"   # 堆固定 512M，初始=最大避免动态扩缩
- name: discovery.type
  value: single-node            # 单节点模式，跳过选举
- name: xpack.security.enabled
  value: "false"                # 关闭安全认证

securityContext:
  fsGroup: 1000                 # 关键：ES 进程以 uid 1000 运行，
                                # 挂载卷属组改 1000 才有写权限

resources:
  requests: {memory: "512Mi", cpu: "200m"}
  limits:   {memory: "1Gi", cpu: "1"}   # 堆 512M + 堆外 + 系统 = 1G 上限
```

数据盘用 `emptyDir`（学习环境临时盘），生产用 PVC/本地盘。

### Step 4：部署 Kibana（NodePort）

Kibana 连 ES 靠环境变量：

```yaml
env:
- name: ELASTICSEARCH_HOSTS
  value: "http://elasticsearch:9200"   # Service 名，不是 IP
```

用 NodePort 暴露（避开 Grafana 的 32255，选 30601）。

### Step 5：部署 Filebeat DaemonSet（核心）

三个组件：RBAC + ConfigMap + DaemonSet。

**RBAC**：Filebeat 要读 Pod/命名空间/节点元数据，需要 ServiceAccount + ClusterRole（`get/list/watch` pods/namespaces/nodes）。

**ConfigMap**（filebeat.yml）核心：

```yaml
filebeat.inputs:
- type: container
  paths:
    - /var/log/containers/*.log     # kubelet 汇总的容器日志
  processors:
    - add_kubernetes_metadata:
        host: ${NODE_NAME}          # 环境变量注入所在节点
        matchers:
        - logs_path:
            logs_path: "/var/log/containers/"   # 关键！从 K8s 日志路径提取容器 ID
    - drop_event:                    # 防止自采集反馈环
        when:
          equals:
            kubernetes.namespace: "logging"

output.elasticsearch:
  hosts: ['http://elasticsearch:9200']
  index: "filebeat-%{[agent.version]}"

setup.ilm.enabled: true
setup.ilm.policy_name: "filebeat"
setup.template.name: "filebeat"      # 改了 index 就必须配这两个
setup.template.pattern: "filebeat-*"
```

**DaemonSet 挂载的 hostPath**（只读）：

| 挂载路径 | 作用 |
|---------|------|
| `/var/log/containers` | 容器日志（符号链接） |
| `/var/log/pods` | 实际日志文件（符号链接目标） |
| `/var/lib/docker/containers` | 容器元数据 |

**initContainer**：`filebeat setup --index-management` 先把索引模板 + ILM 策略刷进 ES，主容器再采集。

### Step 6：Kibana Index Pattern + 检索

1. Index Pattern 建 `filebeat-*`，时间字段 `@timestamp`
2. Discover 检索，KQL 过滤：`kubernetes.namespace : "microshop"`、`kubernetes.container.name : "orders"`

**直达 URL**（跳过导航）：
- Index Patterns：`/app/management/kibana/indexPatterns`
- Discover：`/app/discover`

### Step 7：ILM 索引生命周期

见第四节。

---

## 四、ILM 索引生命周期管理

### 4.1 四个阶段

```
hot（热）─→ warm（温）─→ cold（冷）─→ delete（删除）
 写入中      不再写      很少查        到期删掉
```

| 阶段 | 状态 | 单节点集群 |
|------|------|-----------|
| hot | 写入中，rollover 滚动 | ✅ 需要 |
| warm | 不再写，偶尔查 | ❌ 用不上 |
| cold | 很少查，冻结 | ❌ 用不上 |
| delete | 超保留期自动删除 | ✅ 需要 |

单节点小集群只配 hot + delete。warm/cold 需要多节点 + 更多存储分层。

### 4.2 rollover 机制（为什么不是一个大索引一直写）

```
filebeat-7.17.20-000001  ← 当前写入索引（write index）
         │ 涨到 50GB 或满 30 天
         ▼
filebeat-7.17.20-000002  ← 滚动，新索引接管写入
filebeat-7.17.20-000001  ← 只读，开始 delete 倒计时
         │ 3 天后
         ▼
      自动删除 000001
```

好处：索引不会无限变大（大索引查询慢/恢复慢）、按时间滚动、旧索引整删（高效）、磁盘可控。

### 4.3 ILM 三要素关系

```
ILM 策略（规则：hot/delete）
     ▲ 被引用
索引模板 filebeat-7.17.20（匹配 filebeat-*）
     │ 新索引匹配模式
     ▼
新索引自动挂上策略 → managed: true → 进入生命周期
```

看 `_ilm/policy/filebeat` 的 `in_use_by.composable_templates` 字段就能看到这个引用关系。

### 4.4 配置 hot → delete

```bash
curl -XPUT "http://localhost:9200/_ilm/policy/filebeat" -H 'Content-Type: application/json' -d '{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": { "rollover": { "max_size": "50gb", "max_age": "30d" } }
      },
      "delete": {
        "min_age": "3d",
        "actions": { "delete": {} }
      }
    }
  }
}'
```

> ⚠️ **Filebeat 默认策略只有 hot（rollover），没有 delete**——只管滚动不删除，旧索引无限积累，磁盘照样满。真正的日志保留必须自己加 delete 阶段。

---

## 五、原理图解

### 完整日志链路

```
容器 stdout/stderr
    │
    ▼
kubelet 重定向
    │
    ▼
/var/log/containers/<pod>_<ns>_<container>-<id>.log（宿主机）
    │
    │ Filebeat hostPath 只读挂载
    ▼
Filebeat Pod（DaemonSet，每节点一个）
    │ 1. 读日志
    │ 2. add_kubernetes_metadata：从路径提取容器 ID → 查 K8s API → 加 namespace/pod/labels
    │ 3. drop_event：丢弃 logging 命名空间（避免自采集）
    ▼
Elasticsearch（filebeat-7.17.20 索引）
    │ 倒排索引 + ILM 管理
    ▼
Kibana Discover（KQL 检索）
```

### add_kubernetes_metadata 工作流

```
日志路径 /var/log/containers/orders-xxx_microshop_orders-<64位容器ID>.log
    │
    │ logs_path matcher（从 /var/log/containers/ 提取容器 ID）
    ▼
容器 ID
    │
    │ 查 K8s API（RBAC：get/list/watch pods）
    ▼
Pod 元数据 → 加入日志字段（kubernetes.namespace / pod.name / container.name / labels）
```

---

## 七、常见问题排查

### 问题 1：ES 启动报 `vm.max_map_count is too low`

**现象**：ES Pod 反复 CrashLoopBackOff，日志报 `max virtual memory areas vm.max_map_count [65530] is too low`

**解决**：在 ES 所在节点执行 `sysctl -w vm.max_map_count=262144` + 写入 `/etc/sysctl.conf` 永久生效。

### 问题 2：ES 启动报 `Permission denied` / 写不进 data 目录

**现象**：ES Pod 起不来，日志有权限错误

**原因**：ES 进程以 uid 1000（elasticsearch 用户）运行，挂载卷默认 root 属主，写不进去

**解决**：Pod `securityContext.fsGroup: 1000`，让 K8s 把卷的属组改成 1000。

### 问题 3：Filebeat 日志刷 `Error extracting container id - source value does not contain matcher's logs_path`

**现象**：每条日志都报这个 ERROR，且 Kibana 里日志没有 `kubernetes.*` 字段

**原因**：`add_kubernetes_metadata` 的 matcher 默认从 `/var/lib/docker/containers/` 提取容器 ID，K8s 日志在 `/var/log/containers/`，路径对不上

**解决**：matcher 显式指定 `logs_path: "/var/log/containers/"`。

### 问题 4：Filebeat 日志爆炸（几分钟上百万条）

**现象**：ES 索引文档量暴涨，Filebeat 日志大量报错

**原因**：**自采集反馈环**——Filebeat 采集了它自己的日志（含报错），报错又写进自己的日志，再被采集，无限循环

**解决**：加 `drop_event` 丢弃采集器自己所在命名空间（logging）的日志。

### 问题 5：删除索引后 Filebeat 报 `Cannot index event (status=404)`

**现象**：删除 ES 索引后，Filebeat 日志短暂刷 404 警告

**原因**：Filebeat 的写入别名还指向已删除的索引，瞬时 404

**解决**：无需处理，几秒后 ILM/Filebeat 重建索引自动恢复，只丢几条日志。

### 问题 6：worker 节点执行 kubectl 报 `localhost:8080 refused`

**现象**：`kubectl` 在 node1/node2 上执行报 `The connection to the server localhost:8080 was refused`

**原因**：kubectl 只在 master 配了 kubeconfig，worker 没有 `~/.kube/config`，默认连 localhost:8080

**解决**：kubectl 操作统一在 master 上执行；worker 只做节点本地操作（sysctl、docker）。

---

## 八、生产环境建议

1. **ES 集群化**：单节点 yellow 无冗余，生产至少 3 节点（3 master + 数据节点分离），保证 green + 高可用
2. **ES 持久化**：数据盘用本地 SSD/PVC，绝不用 emptyDir；配 `vm.max_map_count` 和 `ulimit` 通过 init 脚本固化
3. **ES 堆内存**：堆不超过物理内存的 50%，且不超过 32GB（JVM 指针压缩边界）；`-Xms` 和 `-Xmx` 一致避免动态扩缩
4. **时钟同步**：日志时间戳依赖节点时钟，所有节点 chrony 同步内网 NTP（本项目 Day 4 上午监控就踩过时钟漂移）
5. **ILM 必须配 delete**：Filebeat 默认策略只滚动不删除，务必自定义 delete 阶段 + 按合规要求设保留期（如 7/30/90 天）
6. **Filebeat 排除自采集**：drop 掉采集器自身命名空间，防止反馈环打爆磁盘
7. **索引模板 + ILM 配合**：确保 Filebeat 走 rollover alias（`setup.ilm.rollover_alias`），索引才能 `managed: true` 被 ILM 接管
8. **日志分级**：生产用 Logstash 或 Filebeat processor 做字段解析，把日志结构化（JSON 字段），才能高效检索和告警
9. **认证**：生产 ES 8.x 必须开 TLS + 用户名密码，Filebeat/Kibana 都配认证，禁止裸奔
