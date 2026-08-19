# Day 1：Docker CE 安装详解

## 环境说明

- 系统：CentOS 7 Mini (Minimal)
- Docker 版本：19.03.15（CentOS 7 兼容性最好的版本）
- 用途：K8s 容器运行时

---

## 一、安装依赖

```bash
yum install -y yum-utils device-mapper-persistent-data lvm2
```

| 包名 | 作用 | 为什么需要 |
|------|------|-----------|
| `yum-utils` | yum 扩展工具集 | 提供 `yum-config-manager` 命令，用来添加 Docker 官方 yum 源 |
| `device-mapper-persistent-data` | 设备映射持久化数据 | Docker 的 devicemapper 存储驱动依赖此包来管理块设备 |
| `lvm2` | 逻辑卷管理器 | Docker 使用 devicemapper 时需要 LVM 来创建精简池（thin pool） |

> 💡 实际上后面配置了 `overlay2` 存储驱动，所以 device-mapper 和 lvm2 更多是兼容性保留。CentOS 7 内核 3.10+ 已原生支持 overlay2。

---

## 二、添加 Docker CE yum 源

```bash
yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
```

### 命令拆解

| 部分 | 含义 |
|------|------|
| `yum-config-manager` | yum 仓库管理命令（来自 yum-utils 包） |
| `--add-repo` | 添加一个新的 yum 仓库定义文件到 `/etc/yum.repos.d/` |

### 为什么用阿里云镜像？

Docker 官方仓库在 `download.docker.com`，国内访问极慢甚至超时。阿里云镜像站同步了 Docker 官方仓库，速度快 10 倍以上。

执行后会在 `/etc/yum.repos.d/` 下生成 `docker-ce.repo`，内容类似：

```ini
[docker-ce-stable]
name=Docker CE Stable - $basearch
baseurl=https://mirrors.aliyun.com/docker-ce/linux/centos/7/$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://mirrors.aliyun.com/docker-ce/linux/centos/gpg
```

---

## 三、安装 Docker CE

```bash
yum install -y docker-ce-19.03.15 docker-ce-cli-19.03.15 containerd.io
```

### 三个包各自干什么

| 包 | 作用 | 包含内容 |
|------|------|---------|
| **docker-ce** | Docker 引擎守护进程 | `dockerd`（Docker Daemon）、systemd 服务文件 |
| **docker-ce-cli** | Docker 命令行工具 | `docker` 命令（客户端），与 dockerd 通过 Unix Socket 通信 |
| **containerd.io** | 容器运行时 | `containerd`、`runc`，负责实际的容器生命周期管理 |

### 为什么锁版本 19.03.15？

CentOS 7 默认内核 3.10.x，较新的 Docker 版本（20.x+）对内核要求更高或存在兼容问题。19.03.15 是：
- CentOS 7 上经过充分验证的最新版本
- 与 Kubernetes 1.23 兼容（K8s v1.24+ 才弃用 dockershim）

### Docker 架构简图

```
docker CLI (客户端)
     │  Unix Socket (/var/run/docker.sock)
     ▼
dockerd (守护进程)
     │  gRPC
     ▼
containerd (容器管理器)
     │  shim
     ▼
runc (OCI 运行时)
     │
     ▼
 容器进程
```

---

## 四、配置 daemon.json

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": ["https://b9pmyelo.mirror.aliyuncs.com"],
  "exec-opts": ["native.cgroupdriver=systemd"],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
EOF
```

### 逐项解释

#### 1. `registry-mirrors` — 镜像加速器

```
https://b9pmyelo.mirror.aliyuncs.com
```

Docker Hub 默认从 `docker.io` 拉取镜像，国内极慢。配置阿里云镜像加速后，所有 `docker pull` 请求先走阿里云缓存。

**获取加速地址**：登录 [cr.console.aliyun.com](https://cr.console.aliyun.com) → 镜像工具 → 镜像加速器。

> ⚠️ 这里用的是公开分享的加速地址，有调用次数限制，正式环境要换成自己申请的那个。

#### 2. `exec-opts: ["native.cgroupdriver=systemd"]` — Cgroup 驱动

这是**最关键的配置**，直接影响 K8s 安装。

| cgroup 驱动 | 管理者 | 问题 |
|------------|--------|------|
| `cgroupfs` | Docker 自己管理 | Docker 和 K8s 各自维护 cgroup 树，资源限制冲突 |
| `systemd` | systemd 统一管理 | K8s kubelet 和 Docker 共用一棵 cgroup 树，不会冲突 |

```
如果 cgroup 驱动不一致：
  Docker 用 cgroupfs
  kubelet 用 systemd
  → kubelet 启动报错: "cgroup driver mismatch"
  → 或者 Pod 资源限制失效
```

#### 3. `log-driver: "json-file"` + `log-opts` — 日志策略

| 参数 | 含义 | 值 |
|------|------|-----|
| `log-driver` | Docker 日志驱动 | `json-file`：每条日志存为 JSON 一行，最通用 |
| `max-size` | 单个日志文件最大 | `100m`：超过 100MB 就切割 |
| `max-file` | 最多保留文件数 | `3`：最多 3 个切割文件，老的自动删 |

**为什么重要**：容器默认**不限制日志大小**。一个疯狂打印日志的容器可以写满整块磁盘。`max-file: 3` + `max-size: 100m` = 每个容器最多占用 300MB 日志空间。

#### 4. `storage-driver: "overlay2"` — 存储驱动

| 驱动 | 原理 | 现状 |
|------|------|------|
| overlay2 | 联合文件系统，多层层叠 | ✅ 推荐，CentOS 7 内核 3.10+ 原生支持 |
| devicemapper | 基于 LVM 精简池 | ❌ 已弃用，性能差 |
| aufs | 早期联合文件系统 | ❌ CentOS 内核不支持 |

overlay2 原理简图：

```
容器层 (可读写)    ← 容器运行时的修改写在这一层
─────────────
镜像层 3           ← COPY 新文件
─────────────
镜像层 2           ← RUN apt install
─────────────
镜像层 1           ← FROM centos:7
─────────────
```

每层只读，容器层可读写。"联合"的意思是：容器视角看到的是所有层叠在一起的结果，但底层镜像不会被修改。

---

## 五、启动 Docker

```bash
systemctl daemon-reload   # 重新加载 systemd 配置（daemon.json 改动后必须执行）
systemctl enable docker --now   # 设置开机自启 + 立即启动（等效于 enable + start）
```

### `enable --now` vs 分开写

```bash
systemctl enable docker    # 只设置开机自启，不启动
systemctl start docker     # 只启动，不设开机自启

systemctl enable docker --now   # 两者合并：开机自启 + 立即启动
```

---

## 六、验证安装

```bash
docker version
```

输出应包含两部分：

| 部分 | 含义 |
|------|------|
| **Client** | `docker` CLI 命令的版本信息 |
| **Server** | `dockerd` 守护进程的版本信息（Engine） |

只有 Server 部分正常显示（没有报 `Cannot connect to the Docker daemon`），才说明 dockerd 在正常运行。

---

## 七、用户组问题（生产常见）

```bash
# 默认只有 root 能执行 docker 命令
docker ps   # 普通用户执行会报: permission denied

# 解决方法：把用户加到 docker 组
usermod -aG docker $USER
# 重新登录后生效

# 原因：/var/run/docker.sock 的属组是 docker，权限 660
ls -la /var/run/docker.sock
# srw-rw---- 1 root docker 0 Aug  9 10:00 /var/run/docker.sock
```

---

## 小结

| 步骤 | 关键点 |
|------|--------|
| 安装依赖 | yum-utils、device-mapper、lvm2 |
| 添加源 | 阿里云镜像，比官方快 10 倍 |
| 安装包 | docker-ce + docker-ce-cli + containerd.io 三层架构 |
| daemon.json | **cgroupdriver=systemd**（K8s 兼容的关键），registry-mirrors，日志限制，overlay2 |
| 启动验证 | `systemctl enable docker --now` + `docker version` |
