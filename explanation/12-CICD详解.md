# GitHub Actions CI/CD 流水线详解

> 完成日期：2026-08-16 | 涉及主机：devops（Runner）、k8s-master（部署入口）、k8s-node2（Harbor）、GitHub

---

## 目标

用 GitHub Actions + 自托管 Runner，打通「git push → 自动构建镜像 → 推 Harbor → K8s 滚动更新」的全自动 CI/CD 流水线，实现改一行代码 → push → 浏览器自动生效，全程无人干预。

---

## 环境

| 主机 | IP | 角色 |
|------|-----|------|
| devops | 192.168.182.167 | 自托管 Runner（Docker + git + ssh） |
| k8s-master | 192.168.182.164 | kubectl 部署入口（runner SSH 到这里） |
| k8s-node2 | 192.168.182.166 | Harbor 镜像仓库 |
| GitHub | - | microshop 私有仓库 + Actions |

---

## 一、CI/CD 架构与私有网络问题（核心）

### 1.1 为什么必须用自托管 Runner

计划里的 CI/CD 流程是「GitHub Actions → docker push Harbor → SSH k8s-master」。但这里有个**致命的网络障碍**：

```
GitHub 云 Runner（跑在公网）
    │ 想 push 到 Harbor（192.168.182.166）
    │ 想 SSH 到 master（192.168.182.164）
    ✗ 192.168.182.x 是 VMware NAT 私有网段，公网路由不到！
```

**GitHub 托管的 runner 跑在公网，够不着内网 Harbor 和 K8s 集群。** 解决方案是**自托管 Runner**——在自己内网的机器上装一个 runner，让流水线在内网里跑：

```
git push → GitHub Actions 触发
    │ 派发任务（走代理）
    ▼
自托管 Runner（devops，内网）
    ├── git clone          （拉代码，走代理）
    ├── docker build       （构建镜像，基础镜像走代理）
    ├── docker push        （推 Harbor，内网直连，NO_PROXY 排除）
    └── ssh master kubectl （滚动更新，内网直连）
```

**关键点**——不是「会不会写 YAML」，而是「懂不懂 CI 系统怎么触达私有基础设施」。

### 1.2 架构全景

```
┌─────────────────────────────────────────────┐
│  GitHub（代码 + Actions 编排）                │
│  git push → 触发 workflow → 派发任务          │
└──────────────────┬──────────────────────────┘
                   │ 任务派发
                   ▼
┌─────────────────────────────────────────────┐
│  自托管 Runner（devops）                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ git clone│→│docker build│→│ docker push  │ │
│  │ 拉代码   │ │ 构建3镜像 │ │ 推 Harbor    │ │
│  └──────────┘ └──────────┘ └──────┬───────┘ │
│                                   │         │
│  ┌──────────────┐                 │         │
│  │ ssh master   │←────────────────┘         │
│  │ kubectl 滚动 │                           │
│  └──────────────┘                           │
└──────────────────┬──────────────────────────┘
                   │ ssh
                   ▼
┌─────────────────────────────────────────────┐
│  k8s-master                                  │
│  kubectl set image → Deployment 滚动更新     │
└─────────────────────────────────────────────┘
```

---

## 二、自托管 Runner 部署

### 2.1 准备（Docker + git）

Runner 需要 Docker（构建）+ git（拉代码）。devops 之前只做 NFS，需新装。**踩坑**：devops 的 DNS 解析不稳定（`Could not resolve host`），需设可靠 DNS（`223.5.5.5`/`114.114.114.114`）。

### 2.2 下载 + 注册 runner

```bash
# 下载 runner（走代理）
curl -o actions-runner.tar.gz -L https://github.com/actions/runner/releases/download/v2.336.0/actions-runner-linux-x64-2.336.0.tar.gz

# 注册（token 从 GitHub Runners 页生成，限时 1 小时 + 单次有效）
./config.sh --url https://github.com/huochesiji-cell/microshop --token <token>
```

**关键**：runner 禁止用 root 运行（`Must not run with sudo`），要建专用非 root 用户 `runner` 并加入 docker 组。

### 2.3 兼容性地狱（CentOS 7 的硬伤）

这是本次最曲折的部分，CentOS 7（已 EOL）跑现代 runner 踩了三重坑：

| 报错 | 根因 | 解决 |
|------|------|------|
| `GLIBCXX_3.4.20/21 not found` | CentOS 7 的 libstdc++（GCC 4.8）太老 | 用 conda-forge 的 `libstdcxx-ng`（兼容 glibc 2.12） |
| `GLIBC_2.25/2.27/2.28 not found` | runner 自带 Node 24 需要 glibc 2.28，CentOS 7 只有 2.17 | **glibc 不能替换**，改用原生 git clone 替代 JS action（避开 Node） |
| libicu 缺失 | .NET runner 依赖 | `yum install libicu` |

**核心教训**：
1. **libstdc++ 可以换**（它是 C++ 库，用 `LD_LIBRARY_PATH` 指向新版本即可）
2. **glibc 不能换**（它是系统最底层的 C 库，升级会让整个系统崩）
3. 现代 runner 整体要求新 glibc → **CentOS 7 的 native runner 本质上是死路**，生产应该用 Docker 容器跑 runner（Ubuntu 基础镜像自带新库）

### 2.4 时钟漂移（反复踩的坑）

runner 的会话 token 带时间戳，时钟漂移导致 token 提前「过期」，GitHub 会判定「runner 注册后一直没连上」而自动删除注册。

**症状**：runner 反复 `registration has been deleted` / `token expired`。

**治本**：装 `open-vm-tools` 同步虚拟机时钟和宿主机（`date -s` 只是临时改）。

---

## 三、workflow YAML 详解

`.github/workflows/deploy.yml`：

```yaml
name: Build and Deploy Microshop

on:
  push:
    branches: [main]          # 推送到 main 触发

jobs:
  build-and-deploy:
    runs-on: self-hosted      # 跑在自托管 runner（内网）
    steps:
      # 1. 拉代码（原生 git clone，避开 JS action 的 Node 依赖）
      - name: Checkout code (native git)
        run: |
          rm -rf ./* .[!.]* 2>/dev/null || true
          git clone --depth 1 https://x-access-token:${{ github.token }}@github.com/huochesiji-cell/microshop.git .

      # 2. 登录 Harbor（密码从 Secret 读取，不硬编码）
      - name: Login to Harbor
        run: echo "${{ secrets.HARBOR_PASSWORD }}" | docker login 192.168.182.166 -u admin --password-stdin

      # 3. 构建 + 推送 3 个镜像（tag 用 commit SHA，不可变可追溯）
      - name: Build and push images
        run: |
          docker build -t 192.168.182.166/library/frontend:${{ github.sha }} frontend/
          docker push 192.168.182.166/library/frontend:${{ github.sha }}
          # ... orders、users 同理

      # 4. SSH 到 master 滚动更新
      - name: Deploy to Kubernetes
        run: |
          ssh -o StrictHostKeyChecking=no root@192.168.182.164 "kubectl set image deploy/frontend frontend=192.168.182.166/library/frontend:${{ github.sha }} -n microshop && ..."
```

### 关键设计点

| 设计 | 原因 |
|------|------|
| `${{ github.sha }}` 做 tag | 每次 push 唯一 tag，不可变、可追溯、触发滚动更新 |
| `secrets.HARBOR_PASSWORD` | 密码不进代码仓库，CI 日志自动打码 |
| `docker login --password-stdin` | 密码走 stdin，不出现在进程列表 |
| 原生 git clone 替代 checkout | 避开 JS action 的 Node 依赖（glibc 坑） |

---

## 五、常见问题排查

### 问题 1：runner 反复 "registration has been deleted"

**现象**：runner 日志反复报 `registration has been deleted from the server`

**根因**：时钟漂移导致 token 过期，runner 断开，GitHub 自动删除「一直没连上」的注册

**解决**：`date -s` 对齐时钟 + 重新注册（`config.sh`），**治本**装 `open-vm-tools`

### 问题 2：`GLIBCXX_3.4.20 not found`（libstdc++ 太老）

**现象**：`./config.sh` 报 libstdc++ 版本缺失

**根因**：CentOS 7 自带 GCC 4.8（GLIBCXX 到 3.4.19），runner 需要 3.4.20+

**解决**：用 conda-forge 的 `libstdcxx-ng`（`micromamba create -p /opt/libstdc -c conda-forge libstdcxx-ng`），复制 `.so.6` 到 `/usr/local/lib64`，`LD_LIBRARY_PATH` 指向它

### 问题 3：`GLIBC_2.28 not found`（glibc 太老，Node 24）

**现象**：workflow 跑 JS action 时 Node 24 报 glibc 版本缺失

**根因**：runner 自带 Node 20/24 需要 glibc 2.28，CentOS 7 只有 2.17，**glibc 无法替换**

**解决**：改用**原生 git clone** 替代 `actions/checkout`（JS action），让 workflow 全程只用 shell + docker + ssh，不碰 Node

### 问题 4：`x509: certificate signed by unknown authority`

**现象**：`docker login Harbor` 报证书错误

**解决**：提取 Harbor 自签证书 → 放 `/etc/docker/certs.d/192.168.182.166/ca.crt` → 重启 Docker

### 问题 5：`git clone` 报 "目标路径 '.' 已经存在"

**现象**：workflow 的 clone 步骤失败

**根因**：自托管 runner 的 workspace 持久化，上次运行残留文件（含隐藏 `.git`）

**解决**：clone 前 `rm -rf ./* .[!.]*` 清空（`.[!.]*` 连隐藏文件一起删）

### 问题 6：新 Pod 卡 ContainerCreating（Calico unauthorized）

**现象**：`networkPlugin cni failed ... connection is unauthorized`

**根因**：calico-node 的 ServiceAccount token 失效

**解决**：`kubectl delete pod -n kube-system -l k8s-app=calico-node --field-selector spec.nodeName=<节点>` 重启 calico-node

---

## 六、生产环境建议

1. **Runner 容器化**：CentOS 7 老系统跑现代 runner 是死路，生产用 Docker 容器（Ubuntu 基础镜像）跑 runner，自带新 glibc/libstdc++
2. **时钟同步**：所有节点装 `open-vm-tools` 或 `chrony`，时钟漂移是运维事故的高发源头
3. **镜像 tag 规范**：用 commit SHA 或语义化版本，禁止 `latest`（不可追溯）
4. **密钥管理**：密码/Token 存 GitHub Secret 或 Vault，绝不硬编码进 git
5. **CI 凭证最小权限**：runner 的 SSH 密钥、Harbor 账号用最小权限，隔离生产集群
6. **推模式 → 拉模式**：生产规模建议用 GitOps（ArgoCD 拉模式），凭证只在集群内
7. **构建缓存**：多阶段构建 + Docker layer cache，加快构建速度
