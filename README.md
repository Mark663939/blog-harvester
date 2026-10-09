# 博主作品采集器

输入一条博主主页链接，自动完成：识别平台 → 采集全部作品 → 提取文案 → 生成概括 → 识别选题 → 写入飞书多维表格。

**全链路 0 元**，跑在 GitHub Actions 上，不需要你的电脑开机。

---

## 成本说明

| 环节 | 用什么 | 费用 |
|---|---|---|
| 运行平台 | GitHub Actions（公开仓库分钟数无限） | 0 元 |
| 浏览器采集 | Playwright + Chromium | 0 元 |
| 文案概括与选题 | 智谱 GLM-4-Flash | 永久免费 |
| 视频语音转写 | 腾讯云录音文件识别 | 每月 10 小时免费额度 |
| 写入表格 | 飞书 bitable API | 完全免费 |

一个月采十几个博主，额度完全够用。

---

## 你需要先准备四样东西

### 1. 智谱 API Key（免费，做概括和选题）

1. 打开 https://open.bigmodel.cn/usercenter/apikeys
2. 注册登录，创建 API Key
3. GLM-4-Flash 是永久免费的，不用充值

### 2. 腾讯云语音识别（做视频口播转写）

1. 打开 https://console.cloud.tencent.com/cam/capi 创建密钥，得到 SecretId 和 SecretKey
2. 打开 https://console.cloud.tencent.com/asr 开通「录音文件识别」（第一次进入点「立即开通」，免费）
3. `TENCENT_ASR_ENGINE_MODEL_ID` 直接填 `16k_zh`（这是录音文件识别 API 的引擎模型类型，不是需要创建的引擎 ID）
4. 免费额度：**每月 10 小时**，不超就是 0 元

> 只想分析图文内容的话，可以不配这个，运行时把 `enable_asr` 关掉。

**关于额度的关键说明：**

- 扣费顺序是「免费额度 → 预付费 → 后付费」，不超 10 小时账单就是 0 元
- **新用户默认关闭后付费**（2022 年 5 月起）。所以超额时是**直接停服，不扣你钱**，表现为视频只保留标题、没有口播文案
- **建议保持后付费关闭**，这样即使撞到额度也不会意外产生账单
- 每次跑完日志里会输出额度报告，告诉你这次用了多少小时、还剩多少、够转多少条视频

```
语音转写额度报告（腾讯云免费额度：每月 10 小时）
  转写成功：3 条
  本次消耗音频时长：4.8 分钟（0.08 小时）
  本月免费额度估算剩余：约 9.9 小时
  按本次用量估算，剩余额度还够转写约 700 条同类视频
```

**用量参考：** 短视频平均 1-2 分钟。每周采 1 个大博主（约 50 条视频）一个月大约消耗 5 小时，**免费额度完全够用**。如果你每周要采 3 个以上博主，可能会超，届时再考虑换硅基流动的无额度方案（`FunAudioLLM/SenseVoiceSmall`，也是免费）。

### 3. 飞书多维表格

1. 打开 https://open.feishu.cn/app 新建企业自建应用
2. 权限管理里开通这几个权限：
   - `bitable:app`（查看、评论、编辑和管理多维表格）
   - `bitable:app:readonly` 不需要，只给上面的就行
3. 把应用加为表格协作者：打开你的多维表格 → 右上角分享 → 添加应用
4. 在「凭证与基础信息」里找到 App ID、App Secret
5. 表格 URL 里 `base/` 后面那串是 app_token，`table/` 后面那串是 table_id

### 4. 建表（字段名必须一字不差）

新建一张多维表格，加这些字段：

| 字段名 | 类型 | 说明 |
|---|---|---|
| 平台 | 单选 | 填两个选项：抖音、小红书 |
| 内容类型 | 单选 | 填两个选项：视频、图文 |
| 作品链接 | 超链接 | |
| 标题 | 文本 | |
| 文案 | 多行文本 | |
| 文案概括 | 多行文本 | |
| 选题 | 单选 | 见下方 |
| 点赞数 | 数字 | |
| 评论数 | 数字 | |
| 收藏数 | 数字 | |
| 分享数 | 数字 | 可选 |
| 发布时间 | 文本 | 可选 |
| 视频时长 | 数字 | 可选，单位秒 |

**「选题」单选字段的选项**，要和 `src/analyzer.py` 里的 `DEFAULT_TOPICS` 保持一致：

```
产品测评 好物推荐 避坑指南 教程干货 使用测评 剧情段子
生活日常 穿搭美妆 美食探店 旅行攻略 健身运动 职场干货
情感话题 热点解读 知识科普 广告推广 其他
```

想改成自己的分类，就改 `DEFAULT_TOPICS` 和飞书里的单选选项，两边保持一致。

> 字段名写错会导致整批写入失败。改字段名的话，记得同步改 `src/models.py` 的 `to_record()`。

---

## 部署步骤

### 第一步：建仓库并上传代码

```bash
git init
git add .
git commit -m "init: 博主作品采集器"
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

**建议设为 Public（公开）**，Actions 分钟数才无限免费。代码本身不含任何密钥，密钥都存在 Secrets 里。

### 第二步：配置 Secrets

仓库页面 → Settings → Secrets and variables → Actions → New repository secret，逐个添加：

| Secret 名字 | 内容 |
|---|---|
| `ZHIPU_API_KEY` | 智谱的 API Key |
| `STATE_DOUYIN` | 抖音登录态 json（下面第三步生成） |
| `STATE_XIAOHONGSHU` | 小红书登录态 json |
| `FEISHU_APP_ID` | 飞书 App ID |
| `FEISHU_APP_SECRET` | 飞书 App Secret |
| `FEISHU_APP_TOKEN` | 表格 app_token |
| `FEISHU_TABLE_ID` | 表格 table_id |
| `TENCENT_SECRET_ID` | 腾讯云 SecretId |
| `TENCENT_SECRET_KEY` | 腾讯云 SecretKey |
| `TENCENT_ASR_ENGINE_MODEL_ID` | ASR 引擎 ID |

### 第三步：生成登录态（唯一需要在自己电脑上做的步骤）

```bash
git clone https://github.com/你的用户名/你的仓库名.git
cd 你的仓库名
pip install playwright
python -m playwright install chromium

python scripts/login.py --platform douyin
python scripts/login.py --platform xiaohongshu
```

脚本会打开浏览器让你扫码登录，登录后在终端按回车。
成功后会在 `.secrets/` 下生成 `state_douyin.json` 和 `state_xiaohongshu.json`。

用记事本打开这些文件，全选复制，粘到对应的 Secret 里。

> ⚠️ **这个 json 含你的登录凭证，不要发给任何人，也不要提交进 git。** `.gitignore` 已经排除了它们。

### 第四步：跑起来

仓库页面 → Actions → 左边选「采集博主作品」→ 右边 Run workflow：

- `profile_url`：填博主主页链接
- `enable_asr`：首次试跑建议填 `false`，跑通了再开
- `limit`：首次填 `5`，只处理 5 条，确认没问题再填 `0` 跑全部
- `skip_detail`：首次填 `true`，跳过详情页抓取，快很多

跑完去飞书表格看数据。

---

## 每 2-4 周要维护一次

**这是唯一的日常维护动作。** 抖音和小红书的登录 cookie 有效期只有 2-4 周，过期后采集会失败（报错提示"被登录墙拦截"）。

过期了就重跑一次第三步，把新的 json 覆盖到 Secret 里。

---

## 本地调试

想先在自己电脑上试跑：

```bash
pip install -r requirements.txt
python -m playwright install chromium

# 把配置写进 .secrets/ 下的同名文件，格式参考 .env.example
cp .env.example .secrets/ZHIPU_API_KEY
# ... 逐个创建，或直接 export 环境变量

# 试跑，不写飞书
python main.py https://www.douyin.com/user/xxx --limit 5 --no-write --dry-run

# 确认没问题，去掉 --dry-run 正式跑
python main.py https://www.douyin.com/user/xxx --limit 5 --no-write
```

**调试技巧：** `--dry-run` 会打开有头浏览器，能直接看到采集过程卡在哪一步。

---

## 命令行参数

```bash
python main.py <主页链接> [选项]

--no-asr        跳过语音转写，快很多
--no-detail     跳过详情页补全
--no-write      只采集，结果存 out/works.json
--limit N       只处理前 N 条
--topics a,b,c  自定义选题分类
--dry-run       有头浏览器，能看到过程
```

---

## 常见问题

**Q：报「被登录墙拦截」**
登录态过期了。重跑 `scripts/login.py` 更新 Secret。

**Q：报「飞书返回错误 1254xxx」**
字段名不匹配。对照上面第 4 步的字段表，检查 `src/models.py` 的 `to_record()`。

**Q：GitHub Actions 提示时长超限**
单个博主作品太多时，先用 `--limit` 分批跑，或者关掉 `enable_asr`（转写最耗时）。

**Q：触发滑块验证**
同一个账号短时间访问太多次会这样。加 `--limit` 少量试跑，或隔一段时间再试。

**Q：能采到作品但文案是空的**
详情页没抓到正文。检查是否被登录墙挡了详情页，或去掉 `--skip_detail`。

**Q：语音转写一直失败**
检查腾讯云是否已开通录音文件识别、引擎 ID 是否填对、免费额度是否用完。

**Q：日志提示「ASR 额度已超」**
本月转写时长超过 10 小时。两个选择：
- 什么都不用做。后付费默认关闭，超额只是停服不扣费，视频会只有标题
- 换成硅基流动的 `FunAudioLLM/SenseVoiceSmall`（也是免费，无额度上限），我可以帮你改

---

## 项目结构

```
main.py                    主流程编排
src/
  models.py                数据模型、平台识别、数字归一化
  collectors/
    base.py                浏览器启动、滚动加载、反爬节奏
    douyin.py              抖音采集
    xiaohongshu.py         小红书采集
  detail.py                详情页补全（正文、发布时间、互动数）
  asr.py                   腾讯云语音转写
  analyzer.py              GLM 概括与选题
  feishu.py                飞书批量写入
scripts/login.py           登录态更新（需本地跑）
.github/workflows/harvest.yml   Actions 工作流
```

---

## 合规提醒

- 采集公开内容用于竞品分析属常见运营手段，但请遵守平台服务条款
- 抖音官方明确禁止自动化采集，小红书也有风控。**建议用小号操作，控制频率，不要跑大规模**
- 采集到的内容版权属于原作者，**不要二次发布**，只做内部分析
- 语音转写和 LLM 概括可能有个别错误，关键数据请回原片核对
