# Harbor 企业级镜像仓库部署详解

> 完成日期：2026-08-11 | 涉及主机：k8s-node2 (192.168.182.166)

---

## 目标

在 k8s-node2 上通过 docker-compose 部署 Harbor v2.7.1 私有镜像仓库，配置 HTTPS 自签证书，实现 K8s 集群内所有节点推送/拉取镜像。

---

## 环境

| 项目 | 值 |
|------|-----|
| 部署主机 | k8s-node2 (192.168.182.166) |
| Harbor 版本 | v2.7.1 |
| 部署方式 | docker-compose（在线版安装器） |
| HTTPS 证书 | 自签 CA 签发，CN=192.168.182.166 |
| 网络 | 代理上网（k8s-node2 Docker 通过 Clash 代理拉取镜像） |

---

## 操作步骤

### 1. 安装 docker-compose

```bash
# 下载 docker-compose v1.29.2（独立二进制，兼容 Docker 19.03）
# 从 GitHub 下载失败 → Windows 浏览器下载 → SCP 传到 k8s-node2
mv /root/docker-compose-Linux-x86_64 /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose
docker-compose --version    # docker-compose version 1.29.2, build 5becea4c
```

> 💡 **为什么选 v1.29.2？**
> Docker Compose v2 是 Docker CLI 插件形式，需要 Docker 20.10+。本项目的 Docker 19.03.15 只能用 v1.x 独立二进制。

### 2. 下载 Harbor 在线安装器

```bash
# 在线版仅 ~10MB（不含镜像，安装时从 Docker Hub 拉）
tar -xzf harbor-online-installer-v2.7.1.tgz -C /root/
# 产物：harbor/harbor.yml.tmpl  install.sh  prepare  common.sh
```

| 版本 | 大小 | 内容 |
|------|------|------|
| offline（离线版） | ~700MB | 二进制 + 所有镜像已打包，不需外网 |
| **online（在线版）** | ~10MB | 仅二进制，安装时 docker pull 镜像 |

本次选在线版 —— 因为 Docker 已配好代理，安装时能正常拉取镜像。

### 3. 生成自签 HTTPS 证书

> ⚠️ **为什么 Harbor 必须 HTTPS？** Docker Registry 协议强制 HTTPS 传输镜像层数据，HTTP 可被中间人篡改。唯一例外是在 `/etc/docker/daemon.json` 里加 `insecure-registries`，但不推荐。

```bash
mkdir -p /data/harbor/certs && cd /data/harbor/certs

# Step 1：生成 CA 私钥（4096 位 RSA）— 模拟"根证书颁发机构"
openssl genrsa -out ca.key 4096

# Step 2：生成 CA 自签名证书（有效期 10 年）
#   -x509    → 输出自签名证书（不发出签名请求）
#   -new     → 新建证书请求
#   -nodes   → 私钥不加密（no DES，否则每次重启要输入密码）
#   -subj    → 直接指定证书主题，跳过交互式提问
openssl req -x509 -new -nodes -key ca.key \
  -subj "/CN=harbor-ca" \
  -days 3650 -out ca.crt

# Step 3：生成 Harbor 服务器私钥
openssl genrsa -out harbor.key 4096

# Step 4：生成证书签名请求（CSR）
#   /CN=192.168.182.166 → 主要访问地址写 IP
openssl req -new -key harbor.key \
  -subj "/CN=192.168.182.166" \
  -out harbor.csr

# Step 5：创建 SAN 扩展文件（多地址支持）
#   subjectAltName = 访问时用 IP 或主机名都能验证通过
#   extendedKeyUsage = serverAuth → 仅用于 TLS 服务器认证
cat > /tmp/harbor.ext << 'EOF'
subjectAltName = IP:192.168.182.166,DNS:k8s-node2,DNS:harbor.local
extendedKeyUsage = serverAuth
EOF

# Step 6：用 CA 签发服务器证书
#   -CAcreateserial → 生成序列号文件，跟踪 CA 签发过的证书
openssl x509 -req -in harbor.csr \
  -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out harbor.crt -days 3650 \
  -extfile /tmp/harbor.ext

# Step 7：验证 SAN 是否包含正确地址
openssl x509 -in harbor.crt -noout -text | grep -A1 "Subject Alternative"
```

### 证书体系架构图

```
┌─────────────────────┐
│     根 CA (ca.crt)   │  ← 自签名，模拟企业根证书
│     /CN=harbor-ca    │    分发到所有 Docker 客户端
└─────────┬───────────┘
          │ 签发（用 ca.key 签名）
          ▼
┌──────────────────────────────┐
│   服务器证书 (harbor.crt)      │
│   /CN=192.168.182.166        │  ← 部署在 Harbor nginx 上
│   SAN: IP:192.168.182.166,   │
│   DNS:k8s-node2,harbor.local │
└──────────────────────────────┘
```

### 4. 配置 harbor.yml

```yaml
# 核心配置项（精简版，学习环境）
hostname: 192.168.182.166          # 用 IP，不需要 DNS

http:
  port: 80                          # 自动 301 跳转到 HTTPS

https:
  port: 443
  certificate: /data/harbor/certs/harbor.crt
  private_key: /data/harbor/certs/harbor.key

harbor_admin_password: Harbor12345  # 首次登录后改掉
data_volume: /data/harbor           # 镜像数据存储路径

# Trivy 关闭（漏洞 DB 也来自 GitHub，网络不通）
trivy:
  skip_update: true
  offline_scan: true
```

### 5. 配置 Docker 走代理（关键突破点）

> 🔥 **本次最大的坑**：国内环境 Docker Hub 直连超时，镜像加速器只缓存热门镜像，Harbor 的专用镜像（`goharbor/*`）全部拉不下来。

```bash
# 为 Docker daemon 注入 HTTP_PROXY 环境变量
# 原理：docker pull 是 daemon 进程执行的，不是 CLI 进程
# 必须通过 systemd drop-in 给 daemon 加代理环境变量
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/http-proxy.conf << 'EOF'
[Service]
Environment="HTTP_PROXY=http://192.168.182.1:7890"
Environment="HTTPS_PROXY=http://192.168.182.1:7890"
Environment="NO_PROXY=localhost,127.0.0.1,192.168.182.0/24"
EOF
systemctl daemon-reload
systemctl restart docker
```

> 💡 **为什么是 `http://192.168.182.1:7890`？** VMware NAT 模式下，`192.168.182.1` 是宿主机在虚拟网段中的地址，Clash 在 Windows 上监听 7890。

### 6. 启动 Harbor

```bash
cd /root/harbor
./install.sh
```

安装脚本自动完成：
1. `prepare` — 读取 `harbor.yml`，渲染 Nginx/Registry/Core 等子配置文件
2. `docker-compose up -d` — 拉取并启动 9 个容器

---

## 原理图解

### Harbor 架构（9 个容器协作）

```
浏览器 / Docker CLI
        │
        │ HTTPS :443
        ▼
   ┌─────────┐
   │  Nginx   │  ← goharbor/nginx-photon（反向代理 + SSL 终结）
   └────┬────┘
        │ 按路径分发
  ┌─────┼─────────┬──────────┬──────────┐
  │     │         │          │          │
  ▼     ▼         ▼          ▼          ▼
┌────┐ ┌──────┐ ┌───────┐ ┌────────┐ ┌──────────┐
│Core│ │Portal│ │Registry│ │Job     │ │RegistryCtl│
│ API│ │ (UI) │ │       │ │Service │ │          │
└──┬─┘ └──────┘ └───┬───┘ └────────┘ └──────────┘
   │                │
   ▼                │
┌──────┐            │  镜像数据
│  DB  │            │
│(PG)  │            ▼
└──────┘     ┌────────────┐
             │ /data/harbor│  ← 宿主机目录（持久化）
             └────────────┘

┌───────┐     ┌────────┐
│ Redis │     │  Log   │   ← 辅助组件
└───────┘     └────────┘
```

### Docker 拉取镜像的完整数据流

```
k8s-master (192.168.182.164)
    │
    │ docker pull 192.168.182.166/library/myapp:v1
    │
    ├─ 1. Docker CLI 读取 /etc/docker/certs.d/192.168.182.166/ca.crt
    │     验证 Harbor 的服务器证书是否由可信 CA 签发
    │
    ├─ 2. HTTPS 连接到 Harbor Nginx :443
    │
    ├─ 3. Harbor Core 检查认证 → 用户/项目/权限
    │
    ├─ 4. Registry 读取镜像层 → /data/harbor 文件系统
    │
    └─ 5. 镜像层逐层传输回 k8s-master Docker daemon
```

### 证书信任链验证过程

```
Docker Client 发起 TLS 握手
    │
    ▼
Harbor 服务器出示 harbor.crt
    │
    ▼
Docker Client 用本地 ca.crt 验证：
    ├─ harbor.crt 的签发者是不是 ca.crt？
    ├─ harbor.crt 的 SAN 是否包含当前请求的 IP/域名？
    └─ harbor.crt 是否在有效期内？
    │
    ├─ ✅ 全部通过 → 建立 TLS，正常通信
    └─ ❌ 任一失败 → x509: certificate signed by unknown authority
```

### Docker daemon 代理注入原理

```
docker pull goharbor/prepare:v2.7.1
    │
    ▼
Docker Daemon（root 进程，systemd 管理）
    │  读取 systemd drop-in:
    │  Environment="HTTP_PROXY=http://192.168.182.1:7890"
    │
    ▼
所有镜像拉取请求 → 192.168.182.1:7890 (Clash)
    │
    ▼
Clash → 机场节点 → Docker Hub → 返回镜像数据
```

---

## 验证方法

```bash
# 1. 所有 Harbor 容器健康（预期 9 个）
docker ps --format "table {{.Names}}\t{{.Status}}"

# 2. Docker 登录成功
docker login 192.168.182.166 -u admin -p Harbor12345

# 3. 推送测试
docker tag hello-world:latest 192.168.182.166/library/hello-world:latest
docker push 192.168.182.166/library/hello-world:latest

# 4. 从另一节点拉取测试
docker pull 192.168.182.166/library/hello-world:latest

# 5. 浏览器访问 Harbor Web UI
# https://192.168.182.166  （admin / Harbor12345）
```

---

## 常见问题排查

### 问题 1：./install.sh 报 "Client.Timeout exceeded"

**现象**：
```
Unable to find image 'goharbor/prepare:v2.7.1' locally
docker: Error: Get https://registry-1.docker.io/v2/: 
  Client.Timeout exceeded while awaiting headers
```

**原因**：国内 Docker Hub 被墙，直连超时

**解决**：
```bash
# 方案 A：给 Docker daemon 配代理（本次采用）
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/http-proxy.conf << 'EOF'
[Service]
Environment="HTTP_PROXY=http://192.168.182.1:7890"
Environment="HTTPS_PROXY=http://192.168.182.1:7890"
EOF
systemctl daemon-reload && systemctl restart docker

# 方案 B：下载离线版安装包
# 镜像已打包在 harbor-offline-installer-v2.7.1.tgz 中，不需外网
```

### 问题 2：Docker 重启后 Harbor 容器全没了

**现象**：`systemctl restart docker` 后 `docker ps` 只有 harbor-log 甚至一个容器都没有

**原因**：Docker daemon 重启时默认停止所有容器，且 docker-compose 的 restart policy 在某些情况下不生效

**解决**：
```bash
# 立即恢复：回到 Harbor 目录重新拉起
cd /root/harbor && docker-compose up -d

# 永久修复：给所有容器加上 always 重启策略
docker update --restart=always $(docker ps -q --filter "name=harbor")
docker update --restart=always nginx redis registry registryctl
```

### 问题 3：docker login 报 connection refused

**现象**：
```
Error response from daemon: Get https://192.168.182.166/v2/: 
  dial tcp 192.168.182.166:443: connect: connection refused
```

**原因**：Harbor 的 nginx 容器没起来或 443 端口没监听

**排查**：
```bash
docker ps | grep nginx          # nginx 容器在不在？
ss -tlnp | grep 443              # 443 端口有没有监听？
docker logs nginx --tail 20     # 查看 nginx 日志
```

### 问题 4：docker push 报 x509: certificate signed by unknown authority

**现象**：推送时报证书不受信任

**原因**：目标 Docker 客户端没有导入 CA 证书

**解决**：
```bash
# 将 CA 证书放到 Docker 的证书信任目录（目录名必须匹配 Registry 地址）
mkdir -p /etc/docker/certs.d/192.168.182.166
scp ca.crt root@目标IP:/etc/docker/certs.d/192.168.182.166/
systemctl restart docker
```

---

## Docker 代理配置详解（补充知识）

这次解决 Docker Hub 拉取失败的方案值得单独记录：

### Docker 的两种代理概念

| 代理类型 | 配置位置 | 作用范围 |
|---------|---------|---------|
| Docker CLI 代理 | 终端环境变量 `HTTP_PROXY` | 仅影响 `docker login` 等 CLI 命令（走 HTTPS API） |
| **Docker Daemon 代理** | systemd drop-in | **影响 `docker pull/push`**（daemon 执行镜像传输） |

> 🔥 **关键认知**：`docker pull` 是由 Docker daemon（root 进程）执行的，不是由当前 shell 执行的。所以终端里 `export HTTP_PROXY=...` 对 `docker pull` 无效，必须给 daemon 进程本身注入环境变量。

### systemd drop-in 配置原理

```
/etc/systemd/system/docker.service.d/http-proxy.conf
    │
    │  systemd 加载 docker.service 时自动合并
    ▼
/lib/systemd/system/docker.service  +  /etc/systemd/system/docker.service.d/*.conf
    │
    │  合并后的完整 service 定义
    ▼
Docker Daemon 进程启动时继承 Environment 变量
```

---

## 生产环境建议

1. **证书管理**：不要用自签证书。内网用企业 CA 签发，公网用 Let's Encrypt（配合 cert-manager 自动续签）
2. **高可用**：学习环境单节点 Harbor，生产应至少 2 副本 + 共享存储（或 S3）
3. **密码安全**：Harbor12345 是默认密码，安装后立即在 Web UI 修改
4. **数据库备份**：Harbor 用内置 PostgreSQL，定期备份 `/data/harbor/database`
5. **存储规划**：`/data/harbor` 会持续增长（镜像越来越多），配好磁盘监控告警
6. **资源限制**：9 个容器约消耗 1.5GB 内存，生产建议 4GB+ 内存独立部署
7. **代理方案**：如果 Docker Hub 持续不通，建议在路由器层面配透明代理，或搭建 Docker Hub 代理缓存（如 Harbor 的 Proxy Cache 功能）
