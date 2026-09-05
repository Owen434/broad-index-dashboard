# broad-index-dashboard

A股 / 基金 / 黄金 的量化看板脚本集合，输出都是自带交互的单文件 HTML（Plotly），
每天由 GitHub Actions 自动跑一遍，结果发布在 GitHub Pages，无需本地部署即可直接看效果。

## 目录结构

```
stocks/   宽基指数看板（K线形态 + 八大指标矩阵 + ZigZag波段看板 + 评分矩阵，四合一）
          含 price_movement_patterns.py（取数 + K线形态识别内核）
funds/    基金指标矩阵 / ZigZag信号 / 评分矩阵 / 风险排名百分位，都基于板块类 ETF
etf/      45只宽基ETF的申赎资金流看板
gold/     COMEX黄金多周期均线与斜率分析
docs/     GitHub Actions 每天自动生成、发布到 Pages 的 HTML（不要手动改这个目录）
```

## 关于示例数据

`funds/funds_universe_example.csv` 只包含公开的板块/宽基 ETF（旅游ETF、酒ETF、消费ETF……），
**不含任何人的真实持仓或自选列表**。想跑自己关注的基金：复制这份 CSV，把
`基金代码`/`基金名称` 换成你自己的标的即可，`类型`列可以继续填「板块」，
也可以自定义别的标签——网页上的分类按钮会跟着数据自动出现。

`stocks/stock_analysis_suite.py` 默认 `BROAD_INDEX_ONLY = True`：只分析约十来个
宽基指数（上证指数、沪深300…），不含个股，也不需要板块成分股 CSV。
想恢复"按板块勾选个股"的完整功能，把这个开关改成 `False`，并自备
`ztjj_board_stocks.csv`（板块成分股清单，本仓库未包含，因为原始数据带基金持仓比例等私有信息）。

## 本地运行

```bash
pip install -r requirements.txt
cd gold && python gold_ma_slope_analyzer.py
cd ../etf && python etf_broadbase_dashboard.py
cd ../funds && python fund_indicators_matrix.py && python fund_score_matrix.py && python fund_riskrank_percentile.py
cd ../stocks && python stock_analysis_suite.py
```

## 在线看板

Pages 开启后（见下面第 5 步），链接固定是这几个（首次 Actions 成功运行前会 404）：

| 看板 | 链接 |
|---|---|
| 宽基指数 · 八大指标矩阵 | https://owen434.github.io/broad-index-dashboard/stock_8indicators_matrix.html |
| 宽基指数 · ZigZag波段看板 | https://owen434.github.io/broad-index-dashboard/stock_zigzag_signal_analyzer.html |
| 宽基指数 · 评分矩阵 | https://owen434.github.io/broad-index-dashboard/stock_scorematrix.html |
| 板块基金 · 八大指标矩阵 | https://owen434.github.io/broad-index-dashboard/fund_indicators_matrix.html |
| 板块基金 · 评分矩阵 | https://owen434.github.io/broad-index-dashboard/fund_score_matrix.html |
| 板块基金 · 风险排名百分位 | https://owen434.github.io/broad-index-dashboard/fund_riskrank_percentile.html |
| 45只宽基ETF · 申赎资金流看板 | https://owen434.github.io/broad-index-dashboard/etf_flow_dashboard.html |
| 黄金多周期均线与斜率 | https://owen434.github.io/broad-index-dashboard/gold_ma_slopes_interactive.html |

## 部署到 GitHub（从零开始，每一步都写清楚点哪里）

有两种把代码传上去的方式，**选一种就行**：

- **方式 A：网页拖拽上传** —— 全程鼠标点，不用装 git、不用碰命令行，推荐给不熟悉命令行的人（下面默认走这个）。
- **方式 B：命令行 `git push`** —— 需要装 git，好处是以后改代码更新更快。想用这个跳到本节最后的"方式 B"部分。

无论哪种方式，第 1 / 3 / 4 / 5 步（新建仓库、Actions 权限、验证、开 Pages）都是一样的，只有"把代码传上去"这一步不同。

### 第 1 步：在 GitHub 上新建一个空仓库

1. 浏览器打开 github.com，登录后点右上角 **+** → **New repository**
2. `Repository name` 填 `broad-index-dashboard`
3. 选 **Public**（要开源必须是 Public，Pages 免费版也要求 Public 仓库）
4. **不要**勾选下面的 "Add a README file"、".gitignore"、"license" 任何一个
5. 点 **Create repository**

### 第 2 步（方式 A）：网页拖拽上传代码

1. 新建好的空仓库页面上，找到 **uploading an existing file** 这个蓝色链接点进去
   （如果没看到，就在仓库页面点 **Add file** → **Upload files**）
2. 打开我发你的 zip，解压到桌面随便一个文件夹（比如桌面上的 `broad-index-dashboard` 文件夹），
   解压后里面应该能直接看到 `stocks/`、`funds/`、`etf/`、`gold/`、`README.md` 这些
3. 用**文件管理器**（Windows 资源管理器 / Mac Finder）打开这个解压后的文件夹，
   **全选里面所有的文件和文件夹**（`Ctrl+A` 或 `Cmd+A`），
   然后一起拖到网页中间那个虚线框里（Chrome / Edge 浏览器支持拖整个文件夹，
   拖上去之后网页会自动把 `stocks/xxx.py` 这样的路径也保留住）
4. **重要**：`.github` 这个文件夹名字前面带点，有些文件管理器默认是隐藏的
   （Windows 需要在"查看"里勾选"隐藏的项目"；Mac 需要按 `Cmd+Shift+.`）。
   一定要确认 `.github/workflows/daily-update.yml` 这个文件也上传上去了，
   不然后面 Actions 那一步会找不到任务。
5. 等所有文件都出现在页面下方的列表里（不再显示"上传中"），
   拉到最下面，`Commit changes` 那里保持默认，点绿色的 **Commit changes** 按钮
6. 提交完刷新一下仓库主页，能看到 `stocks/`、`funds/`、`.github` 这些文件夹就说明成功了

如果拖拽卡住或者 `.github` 文件夹没能一起拖上去，可以分两次上传：先拖 `stocks/`、`funds/`、
`etf/`、`gold/`、根目录的几个文件，提交一次；再单独进 `.github/workflows` 文件夹拖
`daily-update.yml` 上去，提交第二次——效果是一样的，GitHub 不介意分几次提交。

### 第 2 步（方式 B）：命令行 `git push`

*（选了方式 A 的可以跳过这一整段，直接看第 3 步）*

**先确认本地装了 git**：终端里输入 `git --version`，如果提示找不到命令，
去 [git-scm.com](https://git-scm.com/downloads) 下载安装，一路下一步，装完重新打开终端。

**生成个人访问令牌（Personal Access Token）**：`git push` 现在不能直接用密码登录，
要用令牌代替密码，只需要生成一次：
1. github.com 右上角头像 → **Settings**
2. 左侧菜单拉到最下面 → **Developer settings**
3. 左侧 **Personal access tokens** → **Tokens (classic)**
4. 右上角 **Generate new token** → **Generate new token (classic)**
5. `Note` 随便填名字；`Expiration` 选 90 天或 No expiration 都行
6. 权限勾选框里把 **repo** 整组打勾
7. 拉到最下面点 **Generate token**
8. 页面显示一长串 `ghp_` 开头的字符串——**这是唯一能看到它的一次**，先复制存起来

**推送代码**：把 zip 解压到一个文件夹，终端 `cd` 进这个文件夹，依次执行：
```bash
git init
git add .
git commit -m "init: 开源版量化看板"
git branch -M main
git remote add origin https://github.com/Owen434/broad-index-dashboard.git
git push -u origin main
```
`git push` 会弹出登录框：Username 填 GitHub 用户名，Password 粘贴上面那串 `ghp_...` 令牌
（不是你的 GitHub 密码）。

### 第 3 步：给 Actions 开写权限（自动 commit 需要）

1. 仓库页面上方点 **Settings**（仓库自己的设置，要先点进具体这个仓库）
2. 左侧菜单点 **Actions** → **General**
3. 页面拉到最下面，找到 **Workflow permissions** 这一块
4. 选中 **Read and write permissions**（默认是只读的）
5. 点 **Save**

这一步不做的话，第 4 步 Actions 能跑完抓数据，但最后"把 HTML 提交回仓库"那一步会报 403 权限错误。

### 第 4 步：手动跑一次 Actions，确认能跑通

1. 仓库页面上方点 **Actions** 标签
2. 左侧能看到 **Daily data update**，点进去
3. 右侧有个 **Run workflow** 下拉按钮，分支选 `main`，点绿色的 **Run workflow**
4. 页面会出现一条新的运行记录（黄色圆点转圈 = 进行中），点进去能看到每一步的名字
5. 等 3~8 分钟，全部变绿勾 = 成功；出现红叉说明某一步失败，点开那一步展开日志看具体报错

如果第 2 步走的是方式 A（网页上传），务必先确认 **Actions** 标签页里能看到
`Daily data update` 这个任务——如果标签页完全空白，大概率是 `.github/workflows/daily-update.yml`
没有传上去（回第 2 步方式 A 的第 4 条重新检查）。

常见的失败原因是 AKShare 接口偶发限流/超时（工作流里每步都加了 `continue-on-error`，单步失败
不会导致整个流程中断，只是那个看板今天没更新），重新点一次 **Run workflow** 通常就好。

### 第 5 步：开启 GitHub Pages

1. 仓库 **Settings** → 左侧 **Pages**
2. **Source** 选 **Deploy from a branch**
3. **Branch** 选 `main`，右边目录下拉选 **/docs**
4. 点 **Save**
5. 页面顶部会出现一行提示，等 1~2 分钟后刷新，会显示一个链接，格式是
   `https://Owen434.github.io/broad-index-dashboard/`

**注意顺序**：一定要等第 4 步 Actions 至少成功跑完一次（这样 `docs/` 目录里才有真正的 HTML，
而不是只有一个空的 `index.html`），否则打开链接点进去的图表链接会 404。

### 以后怎么更新代码

- **方式 A（网页）**：改完某个文件，回到仓库里对应文件夹，点开那个文件，右上角铅笔图标
  **编辑**，改完拉到最下面 Commit；或者继续用 **Add file → Upload files** 把改过的文件重新
  拖上去覆盖，GitHub 会按同名文件自动覆盖。
- **方式 B（命令行）**：
  ```bash
  git add .
  git commit -m "说明这次改了什么"
  git push
  ```

### 常见报错怎么办

| 报错信息 / 现象 | 原因 | 解决 |
|---|---|---|
| 网页拖拽后有些文件没出现 | 一次拖太多文件，浏览器卡住/漏传 | 分批拖，先传 4 个脚本文件夹，再单独传 `.github/workflows` |
| Actions 标签页是空的，没有 `Daily data update` | `.github/workflows/daily-update.yml` 没传上去 | 检查文件管理器有没有显示隐藏文件（`.github` 是隐藏文件夹） |
| `remote origin already exists`（方式B） | 方式 B 第2条命令重复执行了 | 改成 `git remote set-url origin https://github.com/...` |
| `Support for password authentication was removed`（方式B） | 登录框填了 GitHub 密码而不是令牌 | 密码框里粘贴生成的 `ghp_...` 令牌 |
| `Permission to xxx.git denied`（方式B） | 令牌没勾 `repo` 权限 | 重新生成令牌，勾好 `repo` |
| Actions 最后一步报 `403` / `Permission denied` | 第 3 步的写权限没开 | 回第 3 步，确认选的是 Read and write |
| Pages 链接打开是 `404` | 还没等 Actions 跑完，或 Pages 目录选错 | 确认选的是 `/docs` 不是 `/(root)`，并等第 4 步成功一次 |

## 已知限制

- 数据源基于 AKShare 抓取国内财经网站接口，GitHub Actions 的海外 runner 偶尔会被限流或
  超时；`daily-update.yml` 里每一步都加了 `continue-on-error`，某个脚本失败不会拖垮其余任务，
  但对应看板当天就不会更新，Actions 日志里能看到具体报错。
- 首次运行前 `docs/` 里只有 `index.html`，链接会 404，等 Actions 第一次跑完就正常了。

## License

MIT（如果你想换成别的协议，在这里改）
