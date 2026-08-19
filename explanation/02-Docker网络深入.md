# Day 1：Docker 网络深入

---

## 前置概念：Linux 网络基础

在理解 Docker 网络之前，先记住 Linux 网络的两个核心机制——Docker 的所有网络模式都是基于它们实现的：

### 1. Network Namespace（网络命名空间）

```
宿主机                              容器
┌──────────────────┐          ┌──────────────────┐
│ 网卡: eth0       │          │ 网卡: eth0       │
│ 路由表            │          │ 路由表 (空的)     │
│ iptables 规则     │          │ iptables 规则     │
│ 端口监听: 22,80   │          │ 端口监听: 80      │
└──────────────────┘          └──────────────────┘
   独立的网络栈                    独立的网络栈
```

每个容器有自己的网络 namespace，有独立的网卡、路由表、iptables、端口空间。所以容器内可以监听 80 端口，不会和宿主机 80 端口冲突。

### 2. veth pair（虚拟网线）

veth pair 是一对**连在一起的虚拟网卡**，一头插在容器里，一头插在宿主机桥上。

```
容器内部                宿主机
┌──────────┐           ┌──────────────┐
│ eth0@if9 │◄──────────►│ veth123@if8  │──► docker0 桥 ──► eth0 → 外网
└──────────┘  veth pair └──────────────┘
```

"veth" = Virtual Ethernet。数据从一头进，必定从另一头出，就像一根网线。

---

## Docker 四种网络模式

```
docker network ls 输出：
NETWORK ID     NAME      DRIVER    SCOPE
abc123         bridge    bridge    local    ← 默认模式
def456         host      host      local    ← 共享宿主机网络
ghi789         none      null      local    ← 无网络
```

### 模式 1：Bridge（桥接）— 默认

**原理图：**

```
      容器A (172.17.0.2)        容器B (172.17.0.3)
           │                        │
    ┌──────┴──────┐          ┌──────┴──────┐
    │ vethA       │          │ vethB       │
    └──────┬──────┘          └──────┬──────┘
           │                        │
           └────────┬───────────────┘
                    │
            ┌───────┴───────┐
            │   docker0     │   (虚拟交换机，IP: 172.17.0.1)
            │   172.17.0.1  │
            └───────┬───────┘
                    │ NAT (iptables MASQUERADE)
            ┌───────┴───────┐
            │     eth0      │   宿主机物理网卡
            │  192.168.182.164│
            └───────────────┘
                    │
                 外网
```

**核心特点：**

| 特性 | 说明 |
|------|------|
| 容器 IP | 从 docker0 子网（172.17.0.0/16）动态分配 |
| 容器互通 | 同一 docker0 桥上的容器可以互 ping（通过 docker0 转发） |
| 访问外网 | 通过 iptables MASQUERADE（SNAT），源 IP 被替换为宿主机 IP |
| 外网访问容器 | 需要 `-p 8080:80` 端口映射（iptables DNAT） |

**端口映射原理：**

```bash
docker run -d -p 8080:80 nginx:alpine
```

背后 iptables 做了什么：

```
访问 宿主机IP:8080
       │
       ▼
PREROUTING 链 (nat 表)
  → DOCKER 子链：发现目标是 172.17.0.2:80
  → DNAT：把目标地址从 宿主机:8080 改成 172.17.0.2:80
       │
       ▼
FORWARD 链 (filter 表)
  → DOCKER-USER 子链：允许转发
       │
       ▼
docker0 桥 → veth → 容器内 eth0 → nginx:80
```

**验证命令：**

```bash
# 查看 NAT 表中的 Docker 规则
iptables -t nat -L DOCKER -n

# 看到类似：
# DNAT  tcp  --  0.0.0.0/0  0.0.0.0/0  tcp dpt:8080  to:172.17.0.2:80
```

---

### 模式 2：Host（主机）— 不隔离

```bash
docker run --network host nginx:alpine
```

**原理图：**

```
     宿主机                         容器
┌──────────────────┐          ┌─────────────┐
│ 网卡: eth0       │          │ 无独立网络栈  │
│ 端口: 80         │◄─────────│ 直接复用      │
│ iptables         │          │ 宿主机网络栈  │
└──────────────────┘          └─────────────┘
```

**核心区别：**

| | Bridge 模式 | Host 模式 |
|------|------|------|
| 容器有独立 IP？ | ✅ 172.17.0.x | ❌ 用宿主机 IP |
| 端口冲突？ | 不会（独立 namespace） | **会！**（和宿主机共享端口） |
| 网络性能 | 有 NAT 开销 | 接近原生（无 NAT） |
| 适用场景 | 通用 Web 服务 | 高性能网络服务（如 Istio Sidecar、网络代理） |

**验证：**

```bash
# host 模式启动 nginx
docker run -d --name nginx-host --network host nginx:alpine

# 直接 curl 宿主机 80 端口，不需要 -p 映射
curl http://localhost:80

# 查看：没有单独的容器 IP
docker inspect nginx-host | grep IPAddress
# "IPAddress": ""   ← 空的！
```

---

### 模式 3：Container（容器网络共享）— Sidecar 的基础

```bash
docker run --network container:<目标容器名> ...
```

**原理图：**

```
  容器A (被共享)                容器B (共享 A 的网络)
┌──────────────┐          ┌──────────────┐
│ eth0          │◄─────────│ 直接用 A 的   │
│ 172.17.0.2   │          │ 网卡和端口    │
│ 端口: 80     │          │ 端口: 8080    │← 和 A 共享同一个 namespace
└──────────────┘          └──────────────┘
```

两个容器共享**同一个网络 namespace**，所以：
- 它们有相同的 IP
- 它们可以互相访问 `localhost:端口`
- 端口不能冲突

**用途：** 这就是 Kubernetes **Pod 内多容器共享网络**的底层机制！Pod 中的 sidecar 容器和主容器就是通过这种方式共享网络的。

```
K8s Pod：
┌─────────────────────────┐
│  共享网络 namespace      │
│  ┌─────────┐ ┌────────┐ │
│  │ 主容器   │ │ sidecar│ │
│  │ nginx   │ │ 日志采集 │ │
│  │ :80     │ │ localhost│ │
│  └─────────┘ └────────┘ │
└─────────────────────────┘
```

**验证：**

```bash
# 启动一个 nginx 容器
docker run -d --name web nginx:alpine

# 启动第二个容器，共享 web 的网络
docker run --rm --network container:web alpine wget -qO- http://localhost

# 成功！因为 localhost 在同一个 namespace 里就是 web 容器的 80 端口
```

---

### 模式 4：None — 无网络

```bash
docker run --network none alpine
```

容器只有 `lo` 回环接口，没有任何外部网络。用于：
- 安全隔离（完全不联网的批处理任务）
- 自定义网络（先创建 none，再手动添加网卡）

---

## 网络模式对比总结

| 模式 | 独立 IP | 独立 namespace | 端口需映射 | 性能 | 典型场景 |
|------|------|------|------|------|------|
| Bridge | ✅ | ✅ | ✅ | 一般（有 NAT）| 通用 Web 应用 |
| Host | ❌ | ❌ | ❌ | 最高 | 高性能/Sidecar |
| Container | 共享 | 共享 | ❌ | 一般 | K8s Pod 多容器 |
| None | ❌ | ✅（但空的）| ❌ | 无网络 | 安全隔离 |
| Overlay | ✅ | ✅ | ❌ | 较低（有封包）| 跨主机通信 |

