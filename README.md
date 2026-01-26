# tj-daily-brief

Daily/weekly research brief generator using OpenAlex, Semantic Scholar (S2), Unpaywall sources and OpenRouter LLM summaries.

### Key settings
- `llm.use_llm_brief` (bool): enable LLM generation.
- `llm.openrouter_model`
- `llm.prompt_version` (cache key component)
- `llm.max_concurrency`
- `llm.max_retries`
- `llm.pending_store` (default `data/pending_llm.json`)
- `llm.cache_file` (default `llm_cache.json`)
- `STRICT_MODE` (env): when `true`, any LLM failure causes the run to exit with non-zero status and no email is sent.

## Seeds
`seeds_positive.txt` / `seeds_negative.txt` are used to build seed-based recommendations and conflict checks.  
If `seeds_positive.txt` is empty, the seeds track is disabled and the run falls back to the profile search query; the profile header will show `seeds track disabled (empty seeds_positive)`.

## OpenAlex API key
OpenAlex is moving toward API key requirements; configure `OPENALEX_API_KEY` to avoid quota issues.  
If missing, OpenAlex data is disabled and the run continues with remaining sources only.

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
