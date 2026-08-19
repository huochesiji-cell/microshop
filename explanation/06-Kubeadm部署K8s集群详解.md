# Kubeadm 部署 Kubernetes 集群详解

> 完成日期：2026-08-11 | 涉及主机：k8s-master、k8s-node1、k8s-node2

---

## 目标

使用 kubeadm 部署 1.23.17 版本 Kubernetes 集群（1 Master + 2 Worker），Calico IPIP 模式作为 CNI 网络插件。

---

## 环境

| 主机 | IP | 角色 | CPU | 内存 |
|------|-----|------|-----|------|
| k8s-master | 192.168.182.164 | control-plane | 2C | 4G |
| k8s-node1 | 192.168.182.168 | worker | 4C | 4G |
| k8s-node2 | 192.168.182.166 | worker | 4C | 4G |

> ⚠️ node1 的 IP 在 Day 3 因宿主机重启从 `.165` 变为 `.168`（已配静态 IP），本文环境表用当前 IP。

---

## 为什么选 K8s 1.23.17？

K8s 1.24 起移除了内置 **dockershim**（Docker 作为容器运行时的桥接层）。如果容器运行时是 Docker（本项目就是），用 1.23.x 是最后一个开箱即用的版本。1.24+ 需要额外安装 `cri-dockerd` 适配器。

```
K8s 1.23 及之前           K8s 1.24 及之后
┌─────────┐              ┌─────────┐
│ kubelet │              │ kubelet │
│ dockershim (内置)│      │  CRI 接口 │
└────┬─────┘              └────┬─────┘
     │                        ├── containerd（默认）
     ▼                        ├── cri-o
  Docker                      └── Docker（需 cri-dockerd）
```

---

## 操作步骤

### 1. 环境初始化（3 台节点都执行）

```bash
# ===== 关闭 swap =====
# K8s 调度器假设节点有固定可用内存，swap 会导致 Pod 内存被换出到磁盘，
# kubelet 无法准确追踪实际可用内存，影响调度决策
swapoff -a
sed -i '/swap/d' /etc/fstab        # 永久关闭

# ===== 关闭 SELinux =====
# SELinux 的安全策略会拦截容器对宿主机文件系统的访问
setenforce 0
sed -i 's/SELINUX=enforcing/SELINUX=disabled/' /etc/selinux/config
```

```bash
# ===== 加载内核模块 =====
# br_netfilter：让 iptables 能过滤 Linux bridge 上的流量
#   Pod 通信通过 bridge 转发，没有这个模块 iptables 规则不会生效
modprobe br_netfilter

# ===== 内核参数 =====
cat > /etc/sysctl.d/k8s.conf << 'EOF'
net.bridge.bridge-nf-call-iptables = 1   # bridge 流量经过 iptables
net.bridge.bridge-nf-call-ip6tables = 1  # 同上，IPv6 版本
net.ipv4.ip_forward = 1                  # 允许 IP 转发（节点作为路由器）
EOF
sysctl --system
```

```bash
# ===== 添加 K8s yum 源（阿里云镜像）=====
cat > /etc/yum.repos.d/kubernetes.repo << 'EOF'
[kubernetes]
name=Kubernetes
baseurl=https://mirrors.aliyun.com/kubernetes/yum/repos/kubernetes-el7-x86_64/
enabled=1
gpgcheck=0
EOF
```

```bash
# ===== 安装指定版本的 kubeadm/kubelet/kubectl =====
yum install -y kubeadm-1.23.17 kubelet-1.23.17 kubectl-1.23.17 --disableexcludes=kubernetes
systemctl enable kubelet
```

> ⚠️ **常见坑**：yum 不指定版本号会装最新版（如 v1.28），和集群版本不兼容！
> 本项目遇到的实际问题：k8s-node1 装了 v1.28.2，join 时报 `unknown flag: --network-plugin`。

### 2. 配置 Docker 走代理（3 台节点都要）

3 台节点都在国内，Docker Hub 直连失败。需为每台配置 Docker daemon 代理：

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/http-proxy.conf << 'EOF'
[Service]
Environment="HTTP_PROXY=http://192.168.182.1:7890"   # Windows 宿主机的 Clash 代理
Environment="HTTPS_PROXY=http://192.168.182.1:7890"
Environment="NO_PROXY=localhost,127.0.0.1,192.168.182.0/24"  # 内网流量不走代理
EOF
systemctl daemon-reload && systemctl restart docker
```

> 💡 **为什么是 systemd drop-in 而不是 export？** `docker pull` 由 Docker daemon（root 进程）执行，不是当前 shell。终端里 `export HTTP_PROXY=...` 只对 CLI 命令（如 `docker login`）有效，对 daemon 的 `docker pull` 无效。

### 3. kubeadm init（仅 k8s-master）

```bash
# 预先拉取组件镜像（走代理 + 阿里云镜像站）
kubeadm config images pull \
  --image-repository registry.aliyuncs.com/google_containers \
  --kubernetes-version v1.23.17
```

```
拉取的 7 个镜像：
┌────────────────────────────┬──────────┐
│ 组件                       │ 用途     │
├────────────────────────────┼──────────┤
│ kube-apiserver             │ K8s 核心 API │
│ kube-controller-manager    │ 控制器管理器  │
│ kube-scheduler             │ Pod 调度器    │
│ kube-proxy                 │ 网络代理      │
│ etcd                       │ 集群状态存储  │
│ coredns                    │ 集群内 DNS    │
│ pause                      │ Pod 基础容器  │
└────────────────────────────┴──────────┘
```

```bash
# 初始化集群
kubeadm init \
  --apiserver-advertise-address=192.168.182.164 \  # Master 对外 IP
  --pod-network-cidr=10.244.0.0/16 \               # Pod 网络段（Calico 默认）
  --image-repository=registry.aliyuncs.com/google_containers \
  --kubernetes-version=v1.23.17
```

> `--pod-network-cidr` 必须和后面 Calico 的配置一致，否则 Pod 分配到不存在的 IP 段会导致网络不通。

### 4. 配置 kubectl

```bash
# root 用户
export KUBECONFIG=/etc/kubernetes/admin.conf
echo 'export KUBECONFIG=/etc/kubernetes/admin.conf' >> ~/.bashrc
```

### 5. 安装 Calico CNI 网络插件

```bash
kubectl apply -f https://docs.projectcalico.org/v3.23/manifests/calico.yaml

# 修复 Calico 自动检测到错误网卡的问题
kubectl set env daemonset/calico-node -n kube-system \
  IP_AUTODETECTION_METHOD=interface=ens33
```

> ⚠️ **本项目的坑**：k8s-node2 上有 Harbor 部署时创建的 Docker bridge（`br-dc2a3aab92d5`，IP `172.18.0.1`），Calico 自动检测时误选了这个网卡而非 `ens33`（`192.168.182.166`），导致 readiness probe 失败、Pod 一直 0/1。

### 6. Worker 节点加入集群

```bash
# 在 k8s-node2 和 k8s-node1 上执行
kubeadm join 192.168.182.164:6443 \
  --token lmtnuh.gorb932c08oaamt9 \
  --discovery-token-ca-cert-hash sha256:d4311858da633d3abedd1461ac7009c3abb6a02a1303dfd1bd0cbf86023d5d8c
```

> 💡 **token 有效期 24 小时**，过期后用 `kubeadm token create --print-join-command` 生成新的。

---

## 原理图解

### kubeadm init 流程

```
kubeadm init
    │
    ├─[1] preflight     → 检查 OS、Docker、内存、端口等前置条件
    ├─[2] certs         → 生成所有证书（CA、apiserver、etcd、front-proxy）
    │     └─ 18 个证书文件写入 /etc/kubernetes/pki/
    ├─[3] kubeconfig    → 生成 admin.conf、kubelet.conf 等配置文件
    ├─[4] kubelet-start → 启动 kubelet
    ├─[5] control-plane → 以 Static Pod 方式启动核心组件
    │     └─ /etc/kubernetes/manifests/ 下的 YAML 被 kubelet 自动执行
    ├─[6] etcd          → 启动 etcd（也是 Static Pod）
    ├─[7] upload-config → 将集群配置存为 ConfigMap
    ├─[8] mark-control-plane → 给 Master 节点打标签 + 污点
    ├─[9] bootstrap-token → 生成 Worker 加入用的 token
    └─[10] addons       → 部署 CoreDNS + kube-proxy
```

### 集群网络架构

```
┌────────────────────────────────────────────────┐
│                  物理网络 192.168.182.0/24       │
│   Node ↔ Node 通信走物理网卡 ens33               │
├────────────────────────────────────────────────┤
│              Calico IPIP Tunnel                 │
│   Pod ↔ Pod 跨节点通信走 IPIP 隧道              │
│   IPIP：将 Pod IP 包再封装一层节点 IP            │
├────────────────────────────────────────────────┤
│              Pod 网络 10.244.0.0/16             │
│   每个节点分配一个 /24 子网                      │
│   k8s-master: 10.244.0.0/24                    │
│   k8s-node1:  10.244.1.0/24                    │
│   k8s-node2:  10.244.2.0/24                    │
├────────────────────────────────────────────────┤
│           Service 网络 10.96.0.0/12             │
│   ClusterIP 虚拟 IP，由 kube-proxy 实现        │
│   kube-dns Service: 10.96.0.10                 │
│   kubernetes Service: 10.96.0.1                │
└────────────────────────────────────────────────┘
```

### Calico IPIP 跨节点通信流程

```
k8s-master 上的 Pod A (10.244.0.5)
    │
    │ 发往 k8s-node2 上的 Pod B (10.244.2.8)
    ▼
Linux 路由表：10.244.2.0/24 via 192.168.182.166 tunl0
    │
    ▼
IPIP 封装：原始 IP 包外再套一层
    ┌──────────────────────────────┐
    │ 外层: 192.168.182.164 → 192.168.182.166 │  ← 节点 IP
    │ 内层: 10.244.0.5 → 10.244.2.8           │  ← Pod IP
    └──────────────────────────────┘
    │
    ▼
物理网络传输到 k8s-node2 的 ens33
    │
    ▼
k8s-node2 解封装 → 路由到 Pod B
```

### CoreDNS 服务发现流程

```
Pod A 中的程序请求 http://my-service.default.svc.cluster.local
    │
    │ 1. Pod 的 /etc/resolv.conf 指向 CoreDNS (10.96.0.10)
    ▼
CoreDNS Pod (kube-dns Service)
    │
    │ 2. CoreDNS 查询 K8s API（watch Service 和 Endpoint 变化）
    ▼
返回 my-service 的 ClusterIP (如 10.96.55.123)
    │
    │ 3. 请求发往 ClusterIP
    ▼
kube-proxy (iptables/IPVS 规则)
    │
    │ 4. DNAT 到实际 Pod IP
    ▼
目标 Pod 收到请求并响应
```

---

## 验证方法

```bash
# 1. 所有节点 Ready
kubectl get nodes
# 预期：3 个节点都是 Ready

# 2. 所有系统 Pod 正常运行
kubectl get pods -n kube-system
# 预期：Calico、CoreDNS、etcd、apiserver 等全是 1/1 Running

# 3. 集群内 DNS 解析
kubectl run test-dns --image=busybox:1.28 --rm -it --restart=Never -- nslookup kubernetes.default
# 预期：解析出 10.96.0.1
```

---

## 常见问题排查

### 问题 1：Calico Pod 卡在 Init 或 0/1 Running

**现象**：`calico-node-xxx` 一直 Init:0/3 或 Running 0/1

**排查**：
```bash
# 看 Events，确认是不是拉镜像卡住了
kubectl describe pod -n kube-system calico-node-xxx | tail -10

# 如果是 Pulling image 超时 → Docker 代理没配
docker pull calico/cni:v3.23.5

# 如果是 readiness probe 失败 → 检查 Calico 自动检测的 IP
kubectl logs -n kube-system calico-node-xxx | grep "autodetected"
```

**解决**：强制指定检测网卡
```bash
kubectl set env daemonset/calico-node -n kube-system \
  IP_AUTODETECTION_METHOD=interface=ens33
```

### 问题 2：kubeadm join 失败 /etc/kubernetes/pki/ca.crt already exists

**原因**：上次 join 残留了文件

**解决**：
```bash
kubeadm reset -f
rm -rf /etc/kubernetes /var/lib/kubelet
kubeadm join ...
```

### 问题 3：节点 NotReady

**排查顺序**：
```bash
# 1. kubelet 在运行吗
systemctl status kubelet

# 2. CNI 插件（Calico Pod）在吗
kubectl get pods -n kube-system -o wide | grep calico

# 3. kubelet 日志
journalctl -xeu kubelet --no-pager | tail -20
```

---

## 生产环境建议

1. **控制面高可用**：学习环境单 Master，生产最少 3 个 Master 节点 + 外部 etcd 集群
2. **etcd 备份**：定期 `etcdctl snapshot save`，这是整个集群的命根子
3. **证书管理**：kubeadm 生成的证书 1 年过期，用 `kubeadm certs renew` 续签或部署 cert-manager
4. **IPVS 模式**：集群规模 > 1000 Service 时，kube-proxy 应切到 IPVS 模式（iptables 规则量 O(n) 变成 O(1)）
5. **网络策略**：Calico 支持 NetworkPolicy，生产应按最小权限原则限制 Pod 间通信
