# Gwibot - Documentação e Andamento do Projeto

Documento de acompanhamento contínuo da evolução, arquitetura, decisões técnicas e roadmap do projeto **Gwibot**.

---

## 📌 Visão Geral do Projeto
O **Gwibot** é um hub de inteligência de ofertas, scraping autenticado e automação de afiliados baseado em Python (**FastAPI + Playwright + SQLite**) com interface inspirada no **Linear Design System** (`Linear Deal Intelligence & Affiliate Engine`).

- **Hub no Notion:** [Gwibot - Central do Projeto (Notion)](https://app.notion.com/p/418b67ba10b8813ba2c1ff235f54a3e0?pvs=204)

---

## 🏗️ Estrutura Atual do Repositório

```text
Gwibot/
├── .env.example               # Modelo de variáveis de ambiente do projeto
├── .gitignore                 # Regras completas de exclusão do Git
├── .playwright-browsers/      # Binários de navegadores gerenciados pelo Playwright (ignorado)
├── .session/                  # Estado de sessão autenticada persistida (ignorado)
├── .venv/                     # Ambiente virtual Python (ignorado)
├── app/
│   ├── __init__.py            # Inicialização do pacote
│   ├── config.py              # Configurações gerais, caminhos e variáveis de ambiente
│   ├── logger.py              # Configuração centralizada de logs rotativos
│   ├── main.py                # Servidor FastAPI e endpoints REST
│   ├── models.py              # Modelos de dados e schemas (Pydantic v2)
│   ├── parser.py              # Extração, parsing e normalização de cards do Mercado Livre
│   ├── scraper.py             # Automação Playwright com suporte a sessão e paginação
│   ├── session_manager.py     # Login manual interativo (headed) e persistência de sessão
│   ├── storage.py             # Camada de persistência SQLite (WAL)
│   ├── affiliates/            # Provedores de links de afiliado (Mercado Livre + stubs futuros)
│   ├── cycles/                # Agendador e pipeline de ciclos automatizados
│   ├── outputs/               # Destinos de publicação (Telegram Bot API + WhatsApp futuro)
│   └── sources/               # Fontes de pesquisa (Mercado Livre + Shopee/Amazon futuros)
├── data/                      # Banco de dados SQLite (gwibot.db) (ignorado)
├── logs/                      # Arquivos de log rotativos (scraper.log) (ignorado)
├── static/
│   ├── index.html             # Estrutura SPA estilo Linear (Sidebar + Top Header + 4 Views)
│   ├── styles.css             # Tokens e componentes do Linear Design System (Dark Mode)
│   └── app.js                 # Lógica de UI, atalhos (/ e Ctrl+K), polling e chamadas REST
├── tests/
│   └── test_app.py            # Suíte de testes automatizados (pytest - 13 testes)
├── requirements.txt           # Dependências do projeto
├── run.bat / start.ps1        # Inicializadores para Windows
├── README.md                  # Apresentação do projeto e guia para desenvolvedores
└── PROGRESS.md                # Este documento de acompanhamento contínuo
```

---

## 🎨 Padrão Oficial de Front-End (Linear Design System)
Definido como padrão obrigatório para toda a evolução da interface:
- **Paleta Dark-First de Alto Contraste**:
  - Superfícies: `--background: #08090A`, `--sidebar: #0D0E10`, `--surface: #131416`, `--surface-elevated: #191A1D`, `--surface-hover: #202125`
  - Bordas sutis de `1px`: `--border-subtle: #1C1D20`, `--border: #252629`, `--border-hover: #323438`
  - Acento e Semântica: `--accent: #5E6AD2`, `--success: #4ADE80`, `--warning: #FBBF24`, `--danger: #F87171`, `--info: #60A5FA`
- **Tipografia & Densidade**:
  - `Inter` (`13px` base, pesos `400/450/500/600`) para textos e controles.
  - `JetBrains Mono` (`--font-mono`) para IDs (`MLB...`), preços, métricas, badges de desconto, contadores, atalhos de teclado (`.kbd-shortcut`) e visualizador JSON.
- **Arquitetura de Layout**:
  - Layout fixo `100vw × 100vh` (`.app-layout`) com barra lateral esquerda de `240px` (`.app-sidebar`), cabeçalho compacto de `48px` com breadcrumbs (`.top-header`) e área de conteúdo rolável (`.main-body`).
  - Ícones vetoriais minimalistas no padrão **Lucide** (`14×14px`, `stroke-width="2"`).

---

## 📋 Registro de Andamento (Changelog & Progresso)

### [2026-11-15] - Atualização de Documentação & Preparação para GitHub (Ready to GitHub)
- **Status:** Concluído e verificado com a suíte de testes (`13 passed`).
- **Entregas Realizadas:**
  - Criação do `.gitignore` padrão profissional cobrindo `.venv/`, `.session/`, `data/`, `logs/`, `.playwright-browsers/`, `.env` e caches do Python/Pytest.
  - Criação do `.env.example` para facilitar o onboarding de novos desenvolvedores.
  - Atualização completa do `README.md` com badges dinâmicos, resumo de recursos, arquitetura detalhada, guia passo a passo de instalação/execução, guia de configuração de canais (Telegram / Afiliados) e instruções para a suíte de testes automatizados.
  - Atualização do `PROGRESS.md` refletindo os 13 testes unitários/integrados da suíte e a estrutura limpa e pronta para versionamento no GitHub.

### [2026-11-15] - Consolidação do Front-End Linear & Sistema de Afiliados/Ciclos
- **Status:** Concluído e validado com testes
- **Entregas Realizadas:**
  - Adoção definitiva do **Linear Design System** em `static/index.html`, `static/styles.css` e `static/app.js`.
  - Implementação do fluxo completo `Cycle → Search → Filter → Affiliate Link → Deduplication → Output`.
  - Integração de afiliados do Mercado Livre (`matt_word` / `matt_tool` + Linkbuilder autenticado) e envio real para o **Telegram** (`sendPhoto` + fallback `sendMessage`).
  - Sincronização do schema `CycleRunLog.affiliate_fail_count` entre backend e tabela de histórico do front-end, além de suporte aos tokens `.status-pill.completed`, `.status-pill.partial` e `.status-pill.idle`.
