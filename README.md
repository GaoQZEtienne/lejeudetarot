# 法式塔罗 · 四人牌桌

四位好友在同一个房间进行一局完整的法式塔罗牌。Python 服务端负责规则、计时与计分，浏览器只接收自己的手牌及公共信息。

## 本机打开

当前工作目录已经建立 `.venv` 并安装运行依赖。Windows 下双击 **`start-game.cmd`**，保持窗口打开，再访问：

**http://127.0.0.1:8080/**

也可在 PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe server.py
```

停止服务按 Ctrl+C。启动脚本不自动开启浏览器窗口。127.0.0.1 仅用于本机访问；异地好友需要下文的公网部署。

首次在另一台电脑安装（需要 Python 3.12 或以上）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

macOS/Linux 对应命令：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

## 四人如何玩

1. 四位玩家打开**完全相同的地址**，输入不同昵称。第一位入座者是房主。
2. 四人都连接成功后，房主点击“开始游戏”。
3. 轮到你时选择“不叫”或“Petit ×1”。四人都不叫就重新发牌。
4. 庄家看到公开底牌后，**只从原来 18 张手牌扣六张**；选中牌会上移。确认后收入全部底牌。
5. 轮到你出牌时，点选亮起的合法牌，再点击“出牌”。灰牌不能选；需要出将时可以出小将。
6. 四张牌出齐展示 2 秒，由赢家先出下一墩。可查看上一墩。
7. 第 18 墩结束后显示结算、各人本局与累计成绩。四人在线时房主可开始下一局。

同一浏览器普通窗口的多个标签页共享一个身份。要在一台电脑上模拟四人，请用四个独立浏览器配置文件或不同浏览器；同一隐私模式的多个窗口也可能共享存储，不能算四个独立身份。

## 本房间规则

- 78 张，每人 18 张、底牌 6 张；六轮每人三张后放一张底牌，最后一张允许进入底牌。
- 随机首位叫牌/首攻者；逆时针行动。只开放 Petit（Petite/Prise）×1，首位叫牌者定庄。
- 先扣原手牌再收入底牌，六张底牌不得扣。R、将牌 1、21 和愚人禁扣；原手牌普通可扣花色不足六张，才补扣必要将牌并公开。
- 有首攻花色必须跟；缺门有将必须出将，**不强制压过桌面最大将牌**。
- 愚人可随时出；首出时下一张定花色。平时保留给原阵营，必要时补对方 0.5 分；末墩按 FFT 口径转交对方，保留大满贯例外。
- 0/1/2/3 个 Bout 的目标为 56/51/41/36 分，保留精确半分。基础 25 加得失分差；末将 10，自然大满贯 200；庄家计三份，防守方各一份。
- 检测 Petit sec 自动重发；不开放 Poignée 和宣告 Chelem。

完整规则与设计取舍见 [PLANNING.md](PLANNING.md)。前三项自定义规则覆盖标准玩法；原始 `prompt.md` 保留不改。

## 计时与返回牌局

牌桌右上角的“离开房间”按当前阶段处理：

- **局外（等待开局或结算后）**：清除入座身份，返回空白昵称表单，已入座人数减一。其他人或另一终端可以立即补位；原身份的其他标签页也会退出。房主离开时自动转交房主。新入座者累计成绩从零开始，上一局结算仍显示原参赛者。
- **局内（叫牌、扣牌、出牌及收墩）**：保留身份和手牌，返回带“返回我的座位”的首页。点击即可继续；其他已打开的牌桌标签页不受影响。

主动离开后刷新当前标签页仍停留在首页。局内可点击“重开投票”，发起人计一票；两名不同玩家赞成后立即重新发牌，本局不计分，保留此前累计成绩。每人每局只能赞成一次，第一票不影响当前计时，重开或结算后清空票数。

叫牌 30 秒，扣牌 90 秒，每次出牌 30 秒。超时依次自动不叫、保留已确认的合法暂选并补足六张、随机出一张合法牌。未收到服务器确认的本地操作不算已提交。

刷新、短暂掉线后自动重连，不重置正常回合的时限。掉线玩家会按原计时自动行动；全员离线时暂停，第一位恢复连接后重新给予当前阶段时限。房主在等待开局/结算阶段持续离线 30 秒，会把房主交给在线玩家；当前对局不依赖房主在线。

身份凭据保存在浏览器本地，服务端只持有哈希。保持相同浏览器配置及相同网址（域名、协议和端口），不要清除该网址的存储。换设备或清除存储后不支持自动认领旧座位。

牌局、昵称和累计成绩保存在 `data/room.sqlite3`。服务重启后继续原局，等待连接恢复后重启本阶段计时。运行时备份 SQLite 应使用 SQLite 备份接口；复制文件前应先正常停止服务，以免漏掉 WAL 中的数据。

需要换人或换终端时，在局外用原浏览器点击“离开房间”，然后从新终端填写昵称入座。若已丢失原凭据且确定放弃整个房间：停止服务，把整个 `data` 目录重命名备份为其他名称，再重启。浏览器原凭据会被拒绝并回到入座页。这会开始全新房间，不会恢复备份中的牌局。

## 异地公网部署

### 免费方案：本机 + Tailscale Funnel

不必购买云服务器或域名。开服电脑安装 Tailscale、登录 Personal 套餐并按提示启用 Funnel 后，可把本机 8080 服务转发为 HTTPS 网站；朋友只需浏览器。玩牌期间电脑必须保持开机、联网且不休眠。

先用 `start-game.cmd` 启动游戏，再在 PowerShell 执行：

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --bg http://127.0.0.1:8080
& 'C:\Program Files\Tailscale\tailscale.exe' funnel status
```

`--bg` 使转发由 Tailscale 后台服务保持，不依赖命令窗口。它不会代替 Python 游戏服务，游戏服务窗口仍需保持运行。若提示 `foreground listener already exists for port 443`，先在原先运行 `tailscale funnel 8080` 的窗口按 Ctrl+C，再执行后台命令；不要同时启动两个相同端口的转发。

状态应列出 HTTPS 地址、`Funnel on` 和 `proxy http://127.0.0.1:8080`。当前配置地址为：

**https://kevingao.tailf99ed2.ts.net/**

首次配置时，[官方说明](https://tailscale.com/docs/features/tailscale-funnel#dns-propagation)指出公网 DNS 记录可能需要最多 10 分钟才能出现。能够在安装 Tailscale 的开服电脑访问，不代表外网已通：本机 MagicDNS 可能将域名解析成 `100.x` 私网地址。验证外网应使用未连接 Tailscale 的手机移动网络或好友设备。

排查顺序：本机 `http://127.0.0.1:8080/health` → `funnel status` → 公网 DNS → 外部 HTTPS/WSS。证书申请中或公网 DNS 尚无记录时，反复重置配置不能保证加快恢复。游戏的 8080 只需监听 `127.0.0.1`，无需为 Funnel 改成公网监听。

关闭这个公网入口：

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' funnel --https=443 off
```

Funnel 链接可被公网访问；本游戏仍按四位好友知道链接的设定运行。大家从一开始就使用同一个 HTTPS 地址入座，避免在 localhost 和公网域名之间切换造成座位凭据不共用。

### 可选方案：自有公网主机

需要一台持续运行、支持 WebSocket、具有持久磁盘的公网主机。只上传 HTML 到静态托管无法提供四人同步；Sites 的托管环境不能直接运行此 Python 进程。

可以把项目部署到已有 Linux 主机。Docker 示例：

```sh
docker build -t friends-tarot .
docker volume create tarot-data
docker run -d --name friends-tarot --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v tarot-data:/app/data friends-tarot
```

在主机配置 Caddy 反向代理，将 [deploy/Caddyfile](deploy/Caddyfile) 中的 `tarot.example.com` 换成已指向该主机的真实域名。开放公网 80/443，由反向代理终止 HTTPS 并转发 WebSocket。四位玩家共享同一个 `https://你的域名/`。

运行一个服务进程/一个实例，不要配置多副本。SQLite 数据卷必须持久保存。本期按“只有四位好友知道链接”的需求，不提供账号体系或复杂访问管理；网址本身不是强身份验证。

非 Docker 运行可设置：

| 设置 | 默认 | 用途 |
| --- | --- | --- |
| `HOST` / `--host` | `127.0.0.1` | 监听地址；容器内设 `0.0.0.0` |
| `PORT` / `--port` | `8080` | 监听端口 |
| `TAROT_DB` | 项目 `data/room.sqlite3` | 持久化文件绝对路径 |

健康检查：`GET /health`。WebSocket：`/ws`。反向代理需保留 Host，允许长连接；浏览器会按 HTTPS 自动使用 WSS。昵称及每个动作在服务端校验，网页文件通过明确路由提供，数据库和源码不公开。

自有公网主机方案尚未部署，Docker/Caddy 示例尚未在实际主机验证。Funnel 已配置到上述域名；具体外网连通性须另外验证，不能仅依据本机测试判断。

## 验证

基础规则与真实四客户端联机验收（仅需运行依赖）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

可选 Edge 浏览器验收：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tests/browser_smoke.py
```

浏览器验收使用已安装的 Microsoft Edge，不需要下载额外浏览器；会创建独立临时房间及五个浏览器上下文，验证四人入座、第五人补位、两票重开、离开与恢复，再用真实页面点击走完整局，检查刷新与手机布局，截图保存在 `artifacts/`。测试不会占用真实房间座位或改动其数据库。

实际验收结果见 [VALIDATION.md](VALIDATION.md)。
