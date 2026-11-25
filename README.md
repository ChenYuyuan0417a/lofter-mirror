# LOFTER 博客 → GitHub Pages 全自动镜像

一套零服务器的镜像系统：GitHub Actions 定时把 LOFTER 博客的文章（正文 + 高清原图）抓到仓库里，GitHub Pages 对外只呈现一个极简的纯内容页面。

访客看到的是：图片 + 文章列表。抓取、比对、下载、推送，全部在云端静默完成。

---

## 先说清楚一件事（很重要）

LOFTER 现在对**未登录访客整体加了登录墙**：直接访问 `xxx.lofter.com` 会被 302 跳到 `www.lofter.com/front/login`，老接口（`dwr`、`art/api`、`oldapi`）也全部失效，换 iPhone UA 只能拿到一个写着“ta 还没有发布过文章哦 / 发布了 0 篇文章”的分享落地页。

也就是说：**不带 Cookie 的匿名爬虫现在抓不到任何内容**，这是站点策略，不是代码问题。

本方案的做法是：把你自己账号的 Cookie 存进 GitHub Secrets，Actions 里用无头浏览器带着这个 Cookie 打开博客，像真人一样渲染页面后再提取图文。所以：

- 依然零服务器、全自动、访客无感；
- 唯一的成本是 Cookie 会过期（通常几个月），过期后 Actions 会失败并邮件提醒你，换一条新 Cookie 即可（重做第 3、4 步）。

---

## 目录结构

```
你的仓库/
├── .github/
│   └── workflows/
│       └── update.yml        # 定时任务：跑爬虫 + 自动提交
├── images/                   # 自动生成：按文章 ID 分文件夹存放原图
│   └── 1f0a1_1ca6ef4/
│       ├── 01-xxxxxxxx.jpg
│       └── 02-xxxxxxxx.jpg
├── data.json                 # 自动生成：全量镜像数据（前端唯一数据源）
├── index.html                # 前台页面，纯展示，只有图片和文章
├── main.py                   # 爬虫主程序（不在前端出现）
├── requirements.txt
└── README.md
```

`images/` 和 `data.json` 第一次跑完才会出现，不用手动建。

---

## 一、仓库已经建好了

地址：`https://github.com/ChenYuyuan0417a/lofter-mirror`，代码已全部推上去，这一步可以跳过。

如果你想自己重建一遍：新建 Public 仓库（免费版 Pages 只对公开仓库开放），把 `main.py`、`index.html`、`requirements.txt`、`README.md` 传到根目录，`.github/workflows/update.yml` 放在 `.github/workflows/` 下（`workflows` 是复数，路径错了 Actions 不会出现）。

---

## 二、拿到你的 LOFTER Cookie

1. 用**能正常看到博客**的那个浏览器打开 https://www.lofter.com
2. 按 `F12` 打开开发者工具，切到 **Network（网络）** 面板
3. 刷新页面，随便点一条对 `lofter.com` 的请求
4. 在 **Request Headers（请求头）** 里找到 `Cookie:` 那一行
5. 把冒号后面**一整串**复制下来（形如 `LOFTER_SESSION=xxx; __gdx=yyy; ...`），这就是 Cookie 字符串

> 别用无痕窗口拿，关掉窗口 Cookie 就失效了。

---

## 三、把 Cookie 存进仓库 Secrets

1. 仓库页 → **Settings → Secrets and variables → Actions**
2. 点 **New repository secret**
3. Name 填 `LOFTER_COOKIE`（必须一字不差，全大写）
4. Secret 粘贴刚才那一整串 Cookie
5. 保存

---

## 四、开启 GitHub Pages

1. 仓库页 → **Settings → Pages**
2. **Source** 选 `Deploy from a branch`
3. **Branch** 选 `main`，目录选 `/ (root)`，保存
4. 等 1~2 分钟，页面顶部会出现你的网址：`https://chenyuyuan0417a.github.io/lofter-mirror/`

以后每次 Actions 推了新数据，Pages 会自动重新发布，不用管。

---

## 五、跑第一次

1. 仓库页 → **Actions** 标签
2. 左侧点 **LOFTER Mirror Sync**
3. 右边 **Run workflow** → 分支选 `main` → 绿色按钮
4. 点进这次运行，能看到实时日志：
   - `共发现 N 篇文章链接`
   - `[1/N] https://...lofter.com/post/...`
   - `完成：共 N 篇 | 新增 x / 更新 y / 未变 z / 删除 0`
5. 跑完打开你的 Pages 网址，内容就出来了

第一次是全量抓取，文章多、图多的时候会比较慢（一篇大约 3~8 秒，含随机延时）。之后每轮只下载变动的图片，很快。

---

## 六、定时任务说明

`update.yml` 里这一行决定频率：

```yaml
schedule:
  - cron: "0 */6 * * *"
```

cron 用的是 **UTC 时间**，`0 */6 * * *` = 每 6 小时一次（北京时间 02:00 / 08:00 / 14:00 / 20:00）。

想改成每天一次：`0 18 * * *`（= 北京时间次日 02:00）。
注意 GitHub 的定时任务在高峰期可能延迟几分钟到几十分钟，属正常现象。

---

## 七、同步逻辑（增 / 删 / 改）

`main.py` 每一轮都做全量对齐：

| 源站情况 | 本地动作 | 页面表现 |
| --- | --- | --- |
| 新发布文章 | 新建条目 + 下载图片 | 新卡片出现 |
| 改了标题 / 正文 / 配图 | 覆盖条目，删掉旧图重下 | 卡片内容更新 |
| 删除了文章 | 删除条目 + 删除图片文件夹 | 卡片消失 |
| 某篇本轮抓取报错 | **保留旧版本**，不删 | 页面不变，下轮再试 |
| 本轮枚举数比本地少 | **只增改，不删除** | 防止一次抽风误删全站 |

删除是最危险的动作，所以设了两道闸：只有「本轮确实把列表完整枚举完」且「这篇不是抓取失败」时才会真删。

想强制对齐一次：

```bash
python main.py --force-sync
```

---

## 八、data.json 结构

```json
{
  "blog": {
    "domain": "xinjinjumin904472226641",
    "home": "https://xinjinjumin904472226641.lofter.com/"
  },
  "updated_at": "2026-11-26T20:00:00+08:00",
  "count": 12,
  "posts": [
    {
      "id": "1f0a1_1ca6ef4",
      "url": "https://xinjinjumin904472226641.lofter.com/post/1f0a1_1ca6ef4",
      "title": "文章标题",
      "published_at": "2026-11-01T21:30:00+08:00",
      "published_raw": "2026-11-01 21:30",
      "text": "正文，段落之间用换行分隔",
      "images": ["images/1f0a1_1ca6ef4/01-abc123.jpg"],
      "image_sources": ["https://原图地址.jpg"],
      "cover": "images/1f0a1_1ca6ef4/01-abc123.jpg",
      "fetched_at": "2026-11-26T20:03:11+08:00",
      "content_hash": "…"
    }
  ]
}
```

- `images` 是**本地相对路径**，前端直接引用，不经过 LOFTER，所以没有防盗链问题。
- `image_sources` 保留原图地址，方便溯源；如果某张图下载失败，`images` 里会自动回退成原地址，卡片不会裂。
- `content_hash` 用来判断这篇文章有没有变，没变就不重复下载图片。

---

## 九、本地调试

```bash
pip install -r requirements.txt
python -m playwright install chromium

# Windows PowerShell
$env:LOFTER_COOKIE="你的cookie"; python main.py --limit 3 --debug

# macOS / Linux
LOFTER_COOKIE="你的cookie" python main.py --limit 3 --debug
```

- `--limit 3`：只抓前 3 篇，快速验证
- `--debug`：把每篇文章的 HTML 存到 `debug/`，解析不对时打开看结构
- 预览页面：`python -m http.server 8000`，别直接双击 `index.html`（`file://` 下 fetch 被拦，必然空白）

---

## 十、可选：图片转 Cloudflare R2

图片默认存在仓库里，靠 GitHub Pages 直接提供，够用且免费。仓库有 1GB 软上限，图片特别多时再考虑 R2：

1. Cloudflare 控制台 → R2 → 建 bucket，开 public access
2. 建 API Token（Object Read & Write）
3. 仓库 Secrets 加这 5 个：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`R2_BUCKET`、`R2_PUBLIC_BASE`
4. 取消 `requirements.txt` 里 `boto3` 那行的注释，重跑一次

---

## 十一、常见问题

**Actions 红色，日志写「拿不到文章列表」**
Cookie 失效，或 Actions 的海外出口被 LOFTER 判定为未登录。先换 Cookie；还不行就把任务改到自己电脑上跑（同一份 main.py，设好环境变量即可）。

**Actions 报「一篇文章都没枚举到」**
Cookie 失效，或 LOFTER 改了页面结构。加 `--debug` 跑一次看 `debug/` 里的 HTML；结构变了就把 HTML 发我，我改选择器。

**页面空白**
1. 确认仓库里有 `data.json`（不是空文件）
2. 浏览器控制台看 `data.json` 是不是 404 → 检查 Pages 的 branch 是否 `main` / `root`
3. 确认用的是 `https://chenyuyuan0417a.github.io/lofter-mirror/` 这种 Pages 网址

**图片显示不出来**
看 `images/` 里有没有对应文件；若 `images` 写的是 `https://...lofter.com/...`，说明那张图下载失败走了回退，下一轮自动重试。

**跑得太慢 / 超时**
调小 `SCROLL_ROUNDS`、`DELAY_MIN/MAX`，但别调到 0，容易被限流。

---

## 十二、两点提醒

1. **这只是你自己的博客的备份**。Cookie 属于账号凭据，只放 GitHub Secrets（加密存储、日志自动打码），不要写进代码或提交到仓库。
2. 抓取节奏已做随机延时（1~3 秒），别调到 0 去高频请求。图片转存到你自己的仓库，对外页面不产生任何对 LOFTER 的请求。
