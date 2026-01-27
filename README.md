# Random-Research
Random-Research从设置的主题/关键词/论文出发寻找并总结相关论文，并通过Github Action每天自动发送邮件简报。

Random-Research使用OpenAlex, Semantic Scholar, Unpaywall sources数据库获得某领域/主题的论文，并通过OpenRouter调用大模型进行简要分析，汇总成每日周报自动发送到gmail邮箱。

Daily/weekly research brief generator using OpenAlex, Semantic Scholar (S2), Unpaywall sources and OpenRouter LLM summaries.
## 邮箱
本项目默认用 Gmail 通过 SMTP 发送简报邮件；
为避免直接使用账号登录密码，推荐使用 Gmail App Password（应用专用密码）：
- 先在 Google 账号里开启 2-Step Verification（两步验证），
- 在 App passwords 页面创建一个 “Mail” 类型的 16 位密码
- 上述 16 位密码就是 SMTP 的密码，不是你的 Google 登录密码 

### 邮箱敏感信息配置为Secrets
安全起见，在 GitHub Actions 中，请把敏感信息都放到仓库 Secrets，过程：
- 进入 Settings → 
- Secrets and variables → 
- Actions → 
- New repository secret 
- 创建下列 Secrets（名字必须完全一致）
  - `SMTP_HOST`：smtp.gmail.com
  - `SMTP_PORT`：587
  - `SMTP_USER`：你的发信 Gmail 地址（完整邮箱）
  - `SMTP_PASS`：你生成的 Gmail App Password（16 位） 
  - `TO_EMAIL`：接收简报的邮箱（可与发信箱相同）

## OpenAlex
OpenAlex是项目文献数据的主要来源之一，2026年以后调用OpenAlex数据需要用到其API Key（免费），需要注册OpenAlex https://openalex.org/ 并拿到API Key
- 注册
- 右上角头像
- API
- API Key
### OpenAlex API Key配置为Secrets
- 进入 Settings → 
- Secrets and variables → 
- Actions → 
- New repository secret 
- 创建下列 Secrets（名字必须完全一致）
  - `OPENALEX_API_KEY`: 注册OpenAlex以后拿到的API Key
  - `OPENALEX_MAILTO`: 注册OpenAlex的邮箱

## Semantic Scholar (可选)
Semantic Scholar是项目文献数据的主要来源之一，但是其API KEY申请比较麻烦，因此是可选配置，可以不配置
### Semantic Scholar配置为Secrets (如有)
- 进入 Settings → 
- Secrets and variables → 
- Actions → 
- New repository secret 
- 创建下列 Secrets（名字必须完全一致）
  - `S2_API_KEY`: Semantic Scholar的官方API KEY （可选项）

## Unpaywall
Unpaywall是项目文献数据的主要来源之一，用于获取可用的开源PDF
### Unpaywall配置为Secrets
- 进入 Settings → 
- Secrets and variables → 
- Actions → 
- New repository secret 
- 创建下列 Secrets（名字必须完全一致）
  - `UNPAYWALL_EMAIL`: 真实、可收信的邮箱地址，可以与接收日报的邮箱不一致

## OpenRouter
需要OpenRouter调用大模型对论文摘要进行简要分析，需要提供自有的OpenRouter API KEY
### OpenRouter配置为Secrets
- 进入 Settings → 
- Secrets and variables → 
- Actions → 
- New repository secret 
- 创建下列 Secrets（名字必须完全一致）
  - `OPENROUTER_API_KEY`: 你的OpenRouter API KEY
### OpenRouter模型
- 默认OpenRouter模型调用为gemini-2.5-flash-lite
- 模型修改：
  - 可以在 config.yml 中找到下面语句更改模型  ` openrouter_model: "google/gemini-2.5-flash-lite" `


## 最小实现流程
### 设置日报主题
对于感兴趣的领域，可以设置主题和关键词，设置完成以后每天将定时收到相关主题的最新、经典或者相关的论文推荐
配置方法如下：
- 在 config.yml 文件中，找到 `profiles` ，这个字段下可以设置多个主题；
- 对于每个主题，可以设置
  - `id` : 主题名称
  - `title_cn` : 日报中文名称
  - `query` 下字段 `search_query` : 你想关注的领域/主题，例如 "online monitoring | power device"
  - `keywords` : 你感兴趣的领域的关键词，例如 "thermal impedance"
  - `exclude_keywords` : 你不想收到这些关键词相关的文献
### 偏好论文(Seeds)
有时我们在某个主题下会有感兴趣/偏好的论文，希望从这些文献出发找到与之相关的论文，通过下面的设置可以使用项目的相关功能
- 在 config.yml 文件中，找到 `profiles` ，这个字段下可以设置多个主题；
- 对于每个主题，可以设置
  - `path` : 指向该主题下存放Seeds论文的文件夹，例如 "profiles/example"，项目执行时会进入仓库的 profiles 文件夹下，找到用户规定的文件夹名称 "example"；在规定的文件夹中，项目会找2个文件 seeds_negative.txt (存放你不想看的论文)和 seeds_positive.txt （存放你感兴趣的论文），从中读取论文的doi；
- 设置 `path` 以后， profiles 文件夹中还不存在自定义的文件夹，需要手动创建**同名**文件夹，并在该文件夹中手动添加 seeds_positive.txt 和 seeds_negative.txt 文件（如有），可以参考示例文档的结构
- 在 seeds_positive.txt 中逐行写入你感兴趣的论文doi
- 在 seeds_negative.txt 中逐行写入你不感兴趣的论文doi


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
