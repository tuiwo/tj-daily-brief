# tj-daily-brief

Daily/weekly research brief generator using OpenAlex, Semantic Scholar (S2), Unpaywall sources and OpenRouter LLM summaries.


## 邮箱
本项目默认用 Gmail 通过 SMTP 发送简报邮件；
为避免直接使用账号登录密码，推荐使用 Gmail App Password（应用专用密码）：
- 先在 Google 账号里开启 2-Step Verification（两步验证），
- 在 App passwords 页面创建一个 “Mail” 类型的 16 位密码
- 上述 16 位密码就是 SMTP 的密码，不是你的 Google 登录密码 

### 邮箱敏感信息配置为Secrets
安全起见，在 GitHub Actions 中，请把敏感信息都放到仓库 Secrets，过程：
进入 Settings → Secrets and variables → Actions → New repository secret 创建下列 Secrets（名字必须完全一致）
- SMTP_HOST：smtp.gmail.com
- SMTP_PORT：587
- SMTP_USER：你的发信 Gmail 地址（完整邮箱）
- SMTP_PASS：你生成的 Gmail App Password（16 位） 
- TO_EMAIL：接收简报的邮箱（可与发信箱相同）

## OpenAlex
OpenAlex是项目文献数据的主要来源之一，2026年以后调用OpenAlex数据需要用到其API Key（免费），需要注册OpenAlex（https://openalex.org/）并拿到API Key
注册 → 右上角头像 → API → API Key
### OpenAlex API Key配置为Secrets
进入 Settings → Secrets and variables → Actions → New repository secret 创建下列 Secrets（名字必须完全一致）
- OPENALEX_API_KEY: 注册OpenAlex以后拿到的API Key
- OPENALEX_MAILTO: 注册OpenAlex的邮箱

## Semantic Scholar (可选)
Semantic Scholar是项目文献数据的主要来源之一，但是其API KEY申请比较麻烦，因此是可选配置，可以不配置
### Semantic Scholar配置为Secrets (如有)
进入 Settings → Secrets and variables → Actions → New repository secret 创建下列 Secrets（名字必须完全一致）
- S2_API_KEY: Semantic Scholar的官方API KEY

## Unpaywall
Unpaywall是项目文献数据的主要来源之一，用于获取可用的开源PDF
### Unpaywall配置为Secrets
进入 Settings → Secrets and variables → Actions → New repository secret 创建下列 Secrets（名字必须完全一致）
- UNPAYWALL_EMAIL: 真实、可收信的邮箱地址，可以与接收日报的邮箱不一致

## OpenRouter
需要OpenRouter调用大模型对论文摘要进行简要分析，需要提供自有的OpenRouter API KEY
### OpenRouter配置为Secrets
- OPENROUTER_API_KEY: 你的OpenRouter API KEY
### OpenRouter模型
- 默认OpenRouter模型调用为gemini-2.5-flash-lite
- 模型修改：
  - 可以在config.yml中找到下面语句更改模型  ` openrouter_model: "google/gemini-2.5-flash-lite" `


## 最小实现流程
### 设置日报主题

### 添加感兴趣的论文






## GitHub Actions secrets
Required:
- `OPENROUTER_API_KEY`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `TO_EMAIL`
- `UNPAYWALL_EMAIL`
- `OPENALEX_API_KEY`
- `OPENALEX_MAILTO`

Optional:
- `S2_API_KEY`
