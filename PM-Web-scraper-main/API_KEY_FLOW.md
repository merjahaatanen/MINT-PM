---
title: How API keys connect to MINT (Ollama / Gemini)
---

# API Key Flow — MINT

This shows how the `.env` API keys reach the actual AI providers, and which
features in the app trigger each call.

```mermaid
flowchart LR
    subgraph Browser["Browser (webapp/index.html)"]
        A1["✨ Sort with AI\n(Machine Info tab)"]
        A2["Generate / Update Checklist\n(Guide tab)"]
    end

    subgraph Flask["server.py (Flask)"]
        B1["/api/departments/<dept>/machines/<eq>/parse-notes"]
        B2["/api/departments/<dept>/machines/<eq>/guide"]
        B3["/api/departments/<dept>/machines/<eq>/guide/update"]
    end

    subgraph GE["guide_engine.py"]
        C1["generate_guide()"]
        C2["update_guide()"]
    end

    subgraph AE["analyze_equipment.py"]
        D1["analyze(prompt, model)"]
        D2{"LLM_PROVIDER\nenv var\n(default: ollama\nif OLLAMA_API_KEY set,\nelse gemini)"}
        D3["_analyze_ollama()"]
        D4["_analyze_gemini()"]
    end

    subgraph Env[".env file (gitignored)"]
        E1["OLLAMA_API_KEY"]
        E2["GEMINI_API_KEY / GOOGLE_API_KEY"]
        E3["OLLAMA_MODEL, OLLAMA_URL"]
        E4["GEMINI_MODEL"]
    end

    subgraph PS["PowerShell subprocess\n(Windows TLS/Schannel)"]
        F1["_PS_SCRIPT_OPENAI\nAuthorization: Bearer $OLLAMA_API_KEY"]
        F2["_PS_SCRIPT\nkey query param"]
    end

    G1["Ollama Cloud\nhttps://ollama.com/v1/chat/completions"]
    G2["Google Gemini\ngenerativelanguage.googleapis.com"]

    A1 -->|POST| B1
    A2 -->|generate PUT/POST| B2
    A2 -->|merge new WOs| B3
    B2 --> C1
    B3 --> C2
    B1 --> D1
    C1 --> D1
    C2 --> D1
    D1 --> D2
    D2 -->|ollama| D3
    D2 -->|gemini| D4
    E1 --> D3
    E3 --> D3
    E2 --> D4
    E4 --> D4
    D3 --> F1 --> G1
    D4 --> F2 --> G2
    G1 -->|JSON response| D3
    G2 -->|JSON response| D4
    D3 -->|raw text| B1 & C1 & C2
    D4 -->|raw text| B1 & C1 & C2
```

## Key points

- **Single choke point**: every AI call in the app funnels through
  `analyze_equipment.py`'s `analyze(prompt, model)`. Nothing else touches the
  network directly.
- **Provider selection**: `LLM_PROVIDER` env var picks the provider. If unset,
  it defaults to `"ollama"` **only if `OLLAMA_API_KEY` is present**, otherwise
  falls back to `"gemini"` (`analyze_equipment.py:394-397`).
- **Key never touches the browser**: the API key lives only in the `.env`
  file on the server machine, read via `os.environ.get(...)` in
  `analyze_equipment.py`. The frontend never sees it — it only calls MINT's
  own `/api/...` routes, which are gated by the shared **edit password**
  (`EDIT_PASSWORD`), not the AI provider key.
- **Why PowerShell?** Corporate network resets OpenSSL/Python TLS, so both
  providers are called via a temporary PowerShell script using Windows'
  native Schannel TLS stack (`_PS_SCRIPT_OPENAI` for Ollama's
  OpenAI-compatible endpoint, `_PS_SCRIPT` for Gemini).
- **Two entry points into the LLM**:
  - `/api/.../parse-notes` (server.py) — "Sort with AI" button, sorts messy
    PM notes into contacts/logins/cost/notes.
  - `guide_engine.py`'s `generate_guide()` / `update_guide()` — builds or
    merges machine checklists from unscheduled work orders.
