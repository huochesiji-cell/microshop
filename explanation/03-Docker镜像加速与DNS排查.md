# Day 1：Docker 镜像加速器配置与 DNS 故障排查

## 故障现象

```bash
docker pull nginx:alpine
# Error: Get https://registry-1.docker.io/v2/: net/http: request canceled
#        while waiting for connection (Client.Timeout exceeded)
```

`docker pull` 任何镜像都超时，但宿主机能 ping 通百度。

---

## 排查过程

### 第一步：确认是网络问题还是配置问题

```bash
# 测试外网连通性（排除 VM 完全断网）
ping www.baidu.com              # ✅ 通 → VM 能上网，不是完整的网络故障

# 测试目标站点
curl -v https://registry-1.docker.io  # ❌ 超时 → Docker Hub 被 GFW 封锁
```

**结论**：VM 能上网（百度通），但 Docker Hub (`registry-1.docker.io`) 在墙外，国内直连不了。

### 第二步：确认镜像加速器是否生效

```bash
docker info | grep -A 10 "Registry Mirrors"
```

```
Registry Mirrors:
  https://docker.mirrors.ustc.edu.cn/     ← 配置了，但 docker pull 还是报错
```

加速器在配置里但没用，说明 Docker 试了镜像站后失败，又回退到 Docker Hub。

### 第三步：测试镜像站是否可达

```bash
curl -v https://docker.mirrors.ustc.edu.cn/v2/
# curl: (6) Could not resolve host: docker.mirrors.ustc.edu.cn
```

**关键发现**：DNS 无法解析镜像站域名！但能解析 `baidu.com`。

### 第四步：确认 DNS 服务器

```bash
cat /etc/resolv.conf
# nameserver 192.168.182.2
```

`192.168.182.2` 是 **VMware NAT 网关**（即 Windows 宿主机）。VM 的 DNS 请求流程：

```
VM → VMware NAT DNS 代理(192.168.182.2) → Windows 网卡 DNS → 上游 DNS
                                              ↑
                                    问题：Windows 使用的 DNS
                                    服务器能解析 baidu.com
                                    但不能解析 Docker 镜像站
```

### 第五步：尝试替换为公共 DNS

```bash
echo "nameserver 114.114.114.114" > /etc/resolv.conf
ping 114.114.114.114    # 100% packet loss
```

**关键发现**：VMware NAT 模式下，VM **无法直接访问外网 DNS 服务器**（114 被 NAT 隔离了），只能通过 VMware 网关 `192.168.182.2` 做 DNS 转发。

### 第六步：逐个测试镜像站哪几个能解析

```bash
for url in docker.m.daocloud.io hub-mirror.c.163.com docker.mirrors.ustc.edu.cn \
           mirror.baidubce.com docker.imgdb.de docker.hlmirror.com \
           docker.lms.run func.ink; do
  echo -n "$url → "
  nslookup $url 2>/dev/null | grep "Address:" | tail -1 | awk '{print $2}'
done
```

结果：

| 镜像站 | DNS 解析 | 能解析？ |
|--------|---------|---------|
| docker.m.daocloud.io | 101.132.188.159 | ✅ |
| func.ink | 223.166.198.161 | ✅ |
| b9pmyelo.mirror.aliyuncs.com | 47.97.72.108 | ✅ |
| docker.imgdb.de | 103.224.182.252 | ✅（国外） |
| ustc / 163 / baidu / lms | 解析失败 | ❌ |

### 第七步：挑选能用的镜像站

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://func.ink",
    "https://b9pmyelo.mirror.aliyuncs.com"
  ]
}
```

---

## VMware NAT 网络模型（理解这个很重要）

```
┌────────────────────────────────────────┐
│              Windows 宿主机             │
│                                        │
│   Virtual NIC (VMnet8)                 │
│   192.168.182.1                        │
│       │                                │
│   NAT 服务 + DHCP + DNS 代理            │
│   192.168.182.2  ← VM 的网关/DNS        │
│       │                                │
│   Windows 物理网卡 → 路由器 → 外网       │
└──────────────────────┼────────────────┘
                       │
       ┌───────────────┼───────────────┐
       │               │               │
  ┌────▼────┐    ┌────▼────┐    ┌────▼────┐
  │ VM1     │    │ VM2     │    │ VM3     │
  │ .164    │    │ .165    │    │ .166    │
  │ DNS→.2  │    │ DNS→.2  │    │ DNS→.2  │
  └─────────┘    └─────────┘    └─────────┘
```

### 关键规则

| 限制 | 原因 |
|------|------|
| VM 不能直接访问外网 DNS | NAT 只做了 NAT 转发，不做 DNS 中继 |
| VM 的 DNS 必须用 192.168.182.2 | 这是 VMware 内置的 DNS 代理 |
| DNS 代理的解析能力 = Windows 的 DNS 服务器 | 如果 Windows 的 DNS 解不了，VM 也解不了 |

### 桥接模式 vs NAT 模式

| | NAT 模式 | 桥接模式 |
|------|------|------|
| VM IP 来源 | VMware 私有子网（192.168.182.x）| 路由器 DHCP（和宿主机同网段） |
| VM 上网 | ✅ 经宿主机 NAT | ✅ 直接走路由器 |
| DNS | 依赖 VMware DNS 代理 | 可以用路由器 DNS/任意公共 DNS |
| 宿主机访问 VM | 需要端口映射 | 直接访问 |
| 适合场景 | 学习/测试 | 生产模拟 |

---

## docker pull 的完整路径

```
docker pull nginx:alpine
        │
        ▼
┌──────────────────────┐
│ 1. 尝试 mirror[0]    │  docker.m.daocloud.io
│    有缓存? → 直接拉   │──── 没有 ──▶ 2
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│ 2. 尝试 mirror[1]    │  func.ink
│    有缓存? → 直接拉   │──── 没有 ──▶ 3
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│ 3. 尝试 mirror[2]    │  b9pmyelo.mirror.aliyuncs.com
│    有缓存? → 直接拉   │──── 没有 ──▶ 4
└──────────────────────┘
        │
        ▼
┌──────────────────────┐
│ 4. 回退到 Docker Hub │  registry-1.docker.io
│    直接被 GFW 拦截    │  ← 这才是之前报错的真正原因
└──────────────────────┘
```

**镜像加速器的作用**：在步骤 1-3 中拦截请求。Docker 的机制是：先去每个镜像站找，都找不到才去 Docker Hub。所以只要有一个镜像站能通，就不会走到步骤 4。

---

## 经验教训

1. **Docker pull 超时不等于加速器没配**。可能是配了但镜像站域名解析不了，Docker 静默跳过，最后回退到 Docker Hub 才报超时。

2. **VMware NAT 模式的 DNS 最容易被忽略**。VM 看起来能上网（ping 百度通），但 DNS 解析能力取决于宿主机 Windows 的 DNS 配置。

3. **一次配多个镜像站**。单个镜像站挂了或解析不了，Docker 会自动尝试下一个，增加容错。

4. **docker info 可以验证加速器是否被加载**。`docker info | grep "Registry Mirrors"` — 如果没出来，说明 daemon.json 格式无效。

---

## 多节点同步：node1/node2 拉取失败的排查

### 现象

master 上 `docker pull hello-world` 成功，但 node1/node2 上 `docker pull hello_world`（下划线）失败：

```
Error: Get https://registry-1.docker.io/v2/: net/http: request canceled
```

### 排查

1. **镜像名写错了**：`hello_world`（下划线）≠ `hello-world`（中划线）。Docker Hub 上不存在 `hello_world` 这个镜像，所以 Docker 在所有镜像站都找不到，最终回退到 Docker Hub 被墙拦截。

2. **DNS 也要检查**：每台 VM 都是独立克隆的，`/etc/resolv.conf` 可能不同。

### 解决

```bash
# 1. 确认 DNS
cat /etc/resolv.conf            # 应该是 nameserver 192.168.182.2

# 2. 确认镜像名正确（中划线不是下划线）
docker pull hello-world          # ✅ 正确
docker pull hello_world          # ❌ 不存在

# 3. 如果 DNS 不对
chattr -i /etc/resolv.conf       # 先解锁
echo "nameserver 192.168.182.2" > /etc/resolv.conf
chattr +i /etc/resolv.conf       # 再锁定
systemctl restart docker
```

### 教训

- **镜像名拼写错误也会导致回退到 Docker Hub**——Docker 会先去镜像站找，镜像站 404（not found），然后去 Docker Hub，Docker Hub 被墙，报超时。表面看像网络问题，实际是镜像名不对。
- **多节点操作时逐台验证**，克隆出来的 VM DNS 配置可能不同。
- **`chattr +i` 是把双刃剑**：它可以防止 NetworkManager 覆盖 DNS，但也导致改 DNS 前必须先 `chattr -i` 解锁。
